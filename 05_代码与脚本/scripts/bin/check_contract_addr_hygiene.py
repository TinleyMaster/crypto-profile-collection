#!/usr/bin/env python3
"""合约地址卫生外部看门狗（check_contract_addr_hygiene）——独立单次检查，由 scheduler 每小时调度。

背景（2026-09-27 solana 合约地址脏值修复）：
  core.asset_contract 曾出现 18 行污染：15 行 solana 地址被全小写降格（solana 的 base58
  地址是**大小写敏感**的，全小写后即非有效 mint）、1 行原生 mint（Wrapped SOL）被当普通合约、
  2 行错配（EVM 地址错贴 solana / DefiLlama 的 'token/' 前缀残留）。写入方已逐一加护栏
  （phase_a_build_core 的 POPULATE_FROM_CMC / POPULATE_FROM_DL、phase_chain_contract_backfill
  的 CMC 回填；DexScreener 结构上只写 EVM，CG 步骤对 solana 保留原样）。

  但唯一索引仍为 `UNIQUE (chain, contract_address)`（**大小写敏感**）：若未来再出现降格路径，
  脏小写行会与正确值**并存**而非报冲突覆盖——无法从结构上完全闭合。本脚本即为此残留风险的
  兜底可观测：每小时独立于任何写入方检查，一旦脏值再现即邮件提醒。

检查项（三条信号，任一 > 0 即告警）：
  A 硬信号：chain='solana' 且 contract_address 不满足 base58 形态
           （^[1-9A-HJ-NP-Za-km-z]{32,44}$）或恰为原生 mint（Wrapped SOL / System Program）
           —— 明确不可用的脏值。
  B 软信号：同一 asset_id 内存在「仅大小写不同」的 solana 变体对（a 与 lower(a) 并存）
           —— 大小写敏感唯一约束留下的残留风险抓手，正是 BOOP 类并存形态。
  C 派生不一致：biz.coin_basic.primary_contract_address 与该 asset 在 core.asset_contract 的
           主合约口径（ORDER BY is_primary DESC, contract_id LIMIT 1）不一致
           —— 脏值已污染到早报/看板消费的派生表。

去重：复用 biz.scan_stall_alert（task='contract_addr_dirty'，与 scan / onchain 看门狗同表不同键，
  各自 6h 静默期互不抑制）——同一事件 6h 内只发一封，持续期间每 6 小时重发一封汇总邮件；
  全部干净后发「已恢复」邮件并清空告警时间戳（下次再现立即可告警）。

退出码：0 = 检查本身执行成功（无论是否发现脏值；脏值用邮件+日志表达），
  非 0 仅代表脚本自身异常（DB 连不上等）。scheduler 把非 0 视为「任务失败」并发管理员失败邮件，
  故脏值判断不能借退出码表达，否则每次检查都会误报任务失败。

用法：
    python check_contract_addr_hygiene.py             # 检查 + 告警（cron 用）
    python check_contract_addr_hygiene.py --dry-run   # 只打印判断，不发送不写状态
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg  # noqa: E402
import psycopg.rows  # noqa: E402

from crypto_research.config import get_settings  # noqa: E402

ALERT_TASK_KEY = "contract_addr_dirty"
REALERT_INTERVAL_H = 6
BJ = ZoneInfo("Asia/Shanghai")
# solana base58 形态（不含 0 O I l），合法长度 32–44
SOLANA_ADDR_RE = r"^[1-9A-HJ-NP-Za-km-z]{32,44}$"
# 原生 mint：Wrapped SOL / System Program —— 都不是普通 SPL 代币合约
SOLANA_NATIVE_MINTS = (
    "So11111111111111111111111111111111111111111",
    "11111111111111111111111111111111",
)
# 明细行上限（只影响邮件里的列表长度，计数始终是全量）
MAX_LIST = 20


def _collect_hard(conn) -> list[dict]:
    """A 硬信号：solana 链中形态非法或为原生 mint 的行。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            "SELECT ac.contract_id, ac.asset_id, a.canonical_symbol, ac.contract_address, "
            "ac.source_code, ac.updated_at "
            "FROM core.asset_contract ac "
            "LEFT JOIN core.asset a ON a.asset_id = ac.asset_id "
            "WHERE ac.chain = 'solana' "
            "  AND (ac.contract_address !~ %s OR ac.contract_address = ANY(%s)) "
            "ORDER BY ac.contract_id",
            (SOLANA_ADDR_RE, list(SOLANA_NATIVE_MINTS)))
        return cur.fetchall()


def _collect_case_variant(conn) -> list[dict]:
    """B 软信号：同一 asset 内「仅大小写不同」的 solana 变体对。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            "SELECT a.contract_id, a.asset_id, a.contract_address AS lower_addr, "
            "b.contract_id AS upper_contract_id, b.contract_address AS upper_addr "
            "FROM core.asset_contract a "
            "JOIN core.asset_contract b "
            "  ON b.asset_id = a.asset_id AND b.chain = 'solana' "
            " AND b.contract_id <> a.contract_id "
            " AND b.contract_address = lower(a.contract_address) "
            "WHERE a.chain = 'solana' "
            "  AND a.contract_address <> lower(a.contract_address) "
            "ORDER BY a.asset_id, a.contract_id")
        return cur.fetchall()


def _count_derived_mismatch(conn) -> int:
    """C 派生不一致：coin_basic 主合约地址 ≠ core.asset_contract 主合约口径。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM biz.coin_basic cb "
            "JOIN LATERAL ("
            "  SELECT ac.contract_address FROM core.asset_contract ac "
            "  WHERE ac.asset_id = cb.asset_id AND ac.chain = 'solana' "
            "  ORDER BY ac.is_primary DESC, ac.contract_id LIMIT 1"
            ") canon ON TRUE "
            "WHERE cb.main_chain = 'solana' "
            "  AND cb.primary_contract_address IS DISTINCT FROM canon.contract_address")
        return cur.fetchone()["n"]


def _collect_dirty(conn) -> dict:
    """汇总三类信号，返回 {hard, variant, derived, total}（明细只保留前 MAX_LIST 行）。"""
    hard = _collect_hard(conn)
    variant = _collect_case_variant(conn)
    derived = _count_derived_mismatch(conn)
    return {
        "hard": hard, "variant": variant, "derived": derived,
        "total": len(hard) + len(variant) + derived,
    }


def _get_alert_state(conn) -> datetime | None:
    """读去重键的上次告警时间（None=无记录或从未告警）。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            "SELECT last_email_ts FROM biz.scan_stall_alert WHERE task=%s",
            (ALERT_TASK_KEY,))
        row = cur.fetchone()
    return row["last_email_ts"] if row else None


def _render_html(d: dict) -> str:
    def table(header: list[str], rows: list[list[str]]) -> str:
        th = "".join(
            f"<th style='padding:4px 10px;border:1px solid #ddd;text-align:left'>{h}</th>"
            for h in header)
        trs = "".join(
            "<tr>" + "".join(
                f"<td style='padding:4px 10px;border:1px solid #ddd'>{c}</td>" for c in r)
            + "</tr>" for r in rows)
        return ("<table style='border-collapse:collapse;font-size:13px'>"
                f"<tr>{th}</tr>{trs}</table>")

    blocks: list[str] = []

    if d["hard"]:
        rows = [[
            str(r["asset_id"]), r["canonical_symbol"] or "-",
            f"<code>{r['contract_address']}</code>",
            r["source_code"] or "-",
            r["updated_at"].strftime("%Y-%m-%d %H:%M") if r["updated_at"] else "-",
        ] for r in d["hard"][:MAX_LIST]]
        extra = (f"<p>（仅列前 {MAX_LIST} 行，共 {len(d['hard'])} 行）</p>"
                 if len(d["hard"]) > MAX_LIST else "")
        blocks.append(
            f"<h3 style='margin:12px 0 4px'>A 硬信号 · 形态非法/原生 mint"
            f"（{len(d['hard'])} 行）</h3>"
            + table(["asset_id", "symbol", "contract_address", "source", "updated_at"], rows)
            + extra)

    if d["variant"]:
        rows = [[
            str(r["asset_id"]),
            f"<code>{r['lower_addr']}</code>",
            f"<code>{r['upper_addr']}</code>",
        ] for r in d["variant"][:MAX_LIST]]
        extra = (f"<p>（仅列前 {MAX_LIST} 组，共 {len(d['variant'])} 组）</p>"
                 if len(d["variant"]) > MAX_LIST else "")
        blocks.append(
            f"<h3 style='margin:12px 0 4px'>B 软信号 · 仅大小写不同的并存变体"
            f"（{len(d['variant'])} 组）</h3>"
            + table(["asset_id", "小写（疑脏值）", "原样（疑真值）"], rows)
            + extra)

    if d["derived"]:
        blocks.append(
            f"<h3 style='margin:12px 0 4px'>C 派生不一致 · biz.coin_basic 主合约"
            f"（{d['derived']} 行）</h3>"
            "<p>派生表 <code>biz.coin_basic.primary_contract_address</code> 与 "
            "<code>core.asset_contract</code> 主合约口径不一致，早报/看板已消费到脏值。</p>")

    if not blocks:
        blocks.append("<p>本次检查未发现脏值。</p>")
    return "".join(blocks)


def _send_mail(settings, subject: str, body: str) -> tuple[bool, str]:
    """发看门狗邮件——只发系统管理员（ADMIN_EMAIL），未配置时回退 SMTP_TO。"""
    from crypto_research.clients.notifier import EmailNotifier
    notifier = EmailNotifier(settings)
    if not notifier.configured:
        return False, "SMTP 未配置（缺少 SMTP_HOST/SMTP_USER/SMTP_PASS/SMTP_TO）"
    return notifier.send(subject, body, from_name="合约地址卫生看门狗",
                         to=settings.admin_email or settings.smtp_to)


def main() -> int:
    parser = argparse.ArgumentParser(description="合约地址卫生外部看门狗（solana 脏值）")
    parser.add_argument("--dry-run", action="store_true", help="只打印判断，不发送邮件不写状态")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    with psycopg.connect(settings.database_url, connect_timeout=15) as conn:
        d = _collect_dirty(conn)
        now = datetime.now(timezone.utc)

        print(f"[看门狗] {now:%m-%d %H:%M} UTC 合约地址卫生检查：")
        print(f"  A 硬信号（形态非法/原生 mint）  {len(d['hard']):>5} 行")
        print(f"  B 软信号（大小写并存变体）      {len(d['variant']):>5} 组")
        print(f"  C 派生不一致（coin_basic）      {d['derived']:>5} 行")
        for r in d["hard"][:MAX_LIST]:
            print(f"    [A] asset={r['asset_id']} {r['canonical_symbol'] or '-'} "
                  f"{r['contract_address']} (source={r['source_code']})")
        for r in d["variant"][:MAX_LIST]:
            print(f"    [B] asset={r['asset_id']} {r['lower_addr']} <-> {r['upper_addr']}")

        last_alert = _get_alert_state(conn)

        if d["total"] == 0:
            if last_alert is None:
                print("[看门狗] 无脏值，无历史告警，结束")
                return 0
            gap_h = (now - last_alert).total_seconds() / 3600
            print(f"[看门狗] 已恢复正常（上次告警 {gap_h:.1f}h 前）→ 发恢复邮件")
            if args.dry_run:
                print("[看门狗] dry-run：跳过恢复邮件发送")
                return 0
            ok, msg = _send_mail(
                settings, "✅ 合约地址卫生已恢复",
                "<h2 style='margin:0'>✅ 合约地址卫生已恢复</h2>"
                "<p>告警发出后 core.asset_contract 已无 solana 脏值"
                "（形态非法 / 原生 mint / 大小写并存 / 派生不一致均为 0）。</p>"
                f"<p>检查时间：{datetime.now(BJ):%Y-%m-%d %H:%M}（北京时间）</p>"
                "<p style='color:#999;font-size:12px'>合约地址卫生外部看门狗 · 自动邮件</p>")
            if ok:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE biz.scan_stall_alert SET last_email_ts=NULL, updated_at=NOW() "
                        "WHERE task=%s", (ALERT_TASK_KEY,))
                conn.commit()
                print("[看门狗] 恢复邮件已发送，告警状态已清空")
            else:
                print(f"[看门狗] 恢复邮件发送失败: {msg}", file=sys.stderr)
            return 0

        if last_alert is not None:
            gap_h = (now - last_alert).total_seconds() / 3600
            if gap_h < REALERT_INTERVAL_H:
                print(f"[看门狗] 脏值持续中，距上次邮件仅 {gap_h:.1f}h"
                      f"（<{REALERT_INTERVAL_H}h），去重跳过")
                return 0

        summary = f"A={len(d['hard'])} / B={len(d['variant'])} / C={d['derived']}"
        print(f"[看门狗] 发现脏值（{summary}）"
              + ("（dry-run 不发送）" if args.dry_run else " → 发告警邮件"))
        if args.dry_run:
            return 0

        ok, msg = _send_mail(
            settings, f"🔴 合约地址脏值告警：{summary}",
            "<h2 style='margin:0'>🔴 core.asset_contract 合约地址脏值告警</h2>"
            f"<p>检查时间：{datetime.now(BJ):%Y-%m-%d %H:%M}（北京时间）</p>"
            + _render_html(d)
            + "<p><b>背景</b>：solana 的 base58 地址大小写敏感，全小写降格后即非有效 mint；"
            "唯一索引 <code>UNIQUE (chain, contract_address)</code> 大小写敏感，"
            "故脏值可与正确值并存而不报冲突。写入方已加护栏，本邮件为残留风险的兜底提醒。</p>"
            "<p><b>排查方向</b>：① 查上述行的 <code>source_code</code> 定位写入方；"
            "② 用 Solana RPC <code>getAccountInfo</code> 对照真值；"
            "③ 修数后须同步重算派生表 <code>biz.coin_basic</code>"
            "（<code>phase_a_build_core.py --step coin_basic</code>）。</p>"
            "<p style='color:#999;font-size:12px'>本邮件由 scheduler 独立调度（每小时）；"
            f"告警期间每 {REALERT_INTERVAL_H} 小时重发一次，恢复后自动发送解除通知。</p>")
        if ok:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO biz.scan_stall_alert (task, last_email_ts, updated_at) "
                    "VALUES (%s, NOW(), NOW()) "
                    "ON CONFLICT (task) DO UPDATE SET "
                    "last_email_ts=EXCLUDED.last_email_ts, updated_at=EXCLUDED.updated_at",
                    (ALERT_TASK_KEY,))
            conn.commit()
            print("[看门狗] 告警邮件已发送")
        else:
            print(f"[看门狗] 告警邮件发送失败: {msg}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())