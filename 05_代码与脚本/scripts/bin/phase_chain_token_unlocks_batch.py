"""
代币解锁数据批量采集脚本。
从 tokenomist.ai / tokenomics.com 用 Playwright 爬取解锁时间表。

用法:
    python phase_chain_token_unlocks_batch.py --limit 100
    python phase_chain_token_unlocks_batch.py --limit 0  # 全量

2026-09-15 优化：浏览器搜索频率 i%50→i%200、失败率阈值 30%（原 fail>0 即 exit 1）、
调度提前至每日 07:00（早报快照前），确保早报"即将解锁"板块用当日数据。

2026-09-26（复验 #5）：原候选查询永久排除 crawl_status='ok'，已抓过的资产再无刷新
路径（PONS 停在 09-04，age 533h）。新增 --refresh-days：把「ok 且 updated_at 超 N 天」
的行并入候选池（最旧优先），使解锁数据能随 tokenomics 时间表推进而更新。
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

sys.stdout.reconfigure(line_buffering=True)

import psycopg
import psycopg.rows

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection


_PENDING_EXCLUDE = """
            AND NOT EXISTS (
                SELECT 1 FROM biz.asset_token_unlocks u
                WHERE u.asset_id = a.asset_id
                  AND (u.crawl_status = 'ok'
                       OR (u.crawl_status = 'not_found'
                           AND u.last_attempt_at > NOW() - INTERVAL '30 day')
                       OR (u.crawl_status = 'fail_timeout'
                           AND u.last_attempt_at > NOW() - INTERVAL '7 day')))
"""

# 候选池市值门槛（美元）：主流币解锁数据早已抓完（not_found 墓碑 30 天），
# 剩余候选全是 <15M 的长尾小币，tokenomics.com 命中率仅 ~10%（89/100 not_found），
# 每币 33s 纯浪费 + 累计超时被误判 stuck（09-11~15 连续 failed 根因）。
# 只处理市值 >= 该门槛的资产；门槛可用环境变量 MIN_UNLOCK_MCAP 覆盖，默认 2000 万美元。
MIN_UNLOCK_MCAP = float(os.environ.get("MIN_UNLOCK_MCAP", "20000000"))

# ── 陈旧刷新（复验 #5，2026-09-26）────────────────────────────────────
# 原候选查询用 _PENDING_EXCLUDE 永久排除 crawl_status='ok' 的行，于是「已抓过一次」
# 的资产再无刷新路径：实测 PONS(11114) 停在 2026-09-04（age 533h），且待抓新候选
# 已归零（每日 07:00 任务实为空转），data_freshness.unlock 因此恒 stale。
# 修复：把「ok 且 updated_at 超过 refresh_days 天」的行并入候选池，排在真正的新
# 候选之后，仍受 --limit 约束；refresh_days=0 表示关闭该路径（回到旧行为）。
#
# 稳态时效（预算即可达性）：设可刷新行数 N、每日预算 B、门槛 D，则稳态刷新周期
#     T = max(D, N/B)
# 因为「到龄(D)才进队列」，队列积压时有 T=N/B，否则 T=D。复验要求 T < 168h。
# 实测（2026-09-26）可通过门槛的 ok 行 N=269，调度日预算 B=100 ⇒ N/B≈2.7 天≈65h；
# 故 D 只要 < 7 天即达标。默认取 5 天：比 N/B 宽松（不追求极限新鲜度、少些爬取
# 压力），又留有 48h 余量，避免像 D=7 那样恰好卡在 168h 边界上。
DEFAULT_REFRESH_DAYS = int(os.environ.get("UNLOCK_REFRESH_DAYS", "5"))

# 陈旧刷新组的选取条件（与主查询同门槛；仅额外要求 crawl_status='ok' 且已过期）
_REFRESH_SELECT = """
    SELECT a.asset_id, a.canonical_symbol AS symbol, a.canonical_name AS name,
           asm.source_asset_key AS coingecko_id,
           COALESCE(a.market_cap, 0) AS mcap, u.updated_at AS last_updated, 1 AS is_refresh
    FROM biz.asset_token_unlocks u
    JOIN core.asset a ON a.asset_id = u.asset_id
    JOIN (
        SELECT DISTINCT ON (asset_id) asset_id, source_asset_key
        FROM core.asset_source_map
        WHERE source_code = 'cg'
        ORDER BY asset_id, source_asset_key
    ) asm ON asm.asset_id = a.asset_id
    WHERE u.crawl_status = 'ok'
      AND u.updated_at < NOW() - (%s * INTERVAL '1 day')
      AND a.status = 'active'
      AND asm.source_asset_key IS NOT NULL
      AND a.asset_type != 'stablecoin'
      AND a.primary_sector != 'meme'
      AND COALESCE(a.market_cap, 0) >= %s
"""

# 同上，但只取 asset_id（供计数查询 UNION，列数须与主查询一致）
_REFRESH_SELECT_IDS = """
    SELECT a.asset_id AS asset_id, 1 AS is_refresh
    FROM biz.asset_token_unlocks u
    JOIN core.asset a ON a.asset_id = u.asset_id
    JOIN (
        SELECT DISTINCT ON (asset_id) asset_id, source_asset_key
        FROM core.asset_source_map
        WHERE source_code = 'cg'
        ORDER BY asset_id, source_asset_key
    ) asm ON asm.asset_id = a.asset_id
    WHERE u.crawl_status = 'ok'
      AND u.updated_at < NOW() - (%s * INTERVAL '1 day')
      AND a.status = 'active'
      AND asm.source_asset_key IS NOT NULL
      AND a.asset_type != 'stablecoin'
      AND a.primary_sector != 'meme'
      AND COALESCE(a.market_cap, 0) >= %s
"""


def get_pending_assets(conn, limit: int, refresh_days: int = DEFAULT_REFRESH_DAYS) -> list[dict]:
    """获取待采集资产列表（新候选 + 陈旧刷新候选）。

    优先处理高市值、非稳定币、非 meme 的资产，跳过已停用资产，
    提升 tokenomics.com 命中率和批量成功率。
    市值门槛：只处理 >= MIN_UNLOCK_MCAP 的资产（2026-09-15 起），
    长尾小币不收录于 tokenomics.com，抓取纯属浪费（曾 89% not_found）。

    P1-1: not_found 墓碑 30 天冷却；parse_empty 视为待重试（不阻塞）。
    隐患1: fail_timeout 墓碑 7 天冷却，避免主流币反复超时浪费配额。
    复验 #5（2026-09-26）：并入「ok 且 updated_at 超 refresh_days 天」的刷新候选，
    否则已抓过的资产永不更新。刷新组按 updated_at 升序（最旧优先），避免按市值
    排序时长尾资产被结构性饿死；新候选组仍按市值降序（行为不变）。
    """
    refresh_union = ""
    params: list = [MIN_UNLOCK_MCAP]
    if refresh_days and refresh_days > 0:
        refresh_union = "UNION ALL\n" + _REFRESH_SELECT
        params += [refresh_days, MIN_UNLOCK_MCAP]
    params.append(limit)
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            f"""
            WITH cand AS (
                SELECT a.asset_id, a.canonical_symbol AS symbol, a.canonical_name AS name,
                       asm.source_asset_key AS coingecko_id,
                       COALESCE(a.market_cap, 0) AS mcap, NULL::timestamptz AS last_updated,
                       0 AS is_refresh
                FROM core.asset a
                JOIN (
                    SELECT DISTINCT ON (asset_id) asset_id, source_asset_key
                    FROM core.asset_source_map
                    WHERE source_code = 'cg'
                    ORDER BY asset_id, source_asset_key
                ) asm ON asm.asset_id = a.asset_id
                WHERE a.status = 'active'
                  AND asm.source_asset_key IS NOT NULL
                  AND a.asset_type != 'stablecoin'
                  AND a.primary_sector != 'meme'
                  AND COALESCE(a.market_cap, 0) >= %s
                  {_PENDING_EXCLUDE}
                {refresh_union}
            )
            SELECT asset_id, symbol, name, coingecko_id, is_refresh, last_updated
            FROM cand
            ORDER BY is_refresh ASC,
                     COALESCE(last_updated, 'epoch'::timestamptz) ASC,
                     mcap DESC, asset_id ASC
            LIMIT %s
            """,
            tuple(params),
        )
        return cur.fetchall()


def get_total_pending(conn, refresh_days: int = DEFAULT_REFRESH_DAYS) -> tuple[int, int]:
    """返回 (新候选数, 陈旧刷新候选数)。"""
    refresh_union = ""
    params: list = [MIN_UNLOCK_MCAP]
    if refresh_days and refresh_days > 0:
        refresh_union = "UNION ALL\n" + _REFRESH_SELECT_IDS
        params += [refresh_days, MIN_UNLOCK_MCAP]
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH cand AS (
                SELECT a.asset_id AS asset_id, 0 AS is_refresh
                FROM core.asset a
                JOIN (
                    SELECT DISTINCT ON (asset_id) asset_id, source_asset_key
                    FROM core.asset_source_map
                    WHERE source_code = 'cg'
                    ORDER BY asset_id, source_asset_key
                ) asm ON asm.asset_id = a.asset_id
                WHERE a.status = 'active'
                  AND asm.source_asset_key IS NOT NULL
                  AND a.asset_type != 'stablecoin'
                  AND a.primary_sector != 'meme'
                  AND COALESCE(a.market_cap, 0) >= %s
                  {_PENDING_EXCLUDE}
                {refresh_union}
            )
            SELECT COUNT(*) FILTER (WHERE is_refresh = 0), COUNT(*) FILTER (WHERE is_refresh = 1)
            FROM cand
            """,
            tuple(params),
        )
        row = cur.fetchone()
        return int(row[0] or 0), int(row[1] or 0)


def _mark_fail_timeout(conn, asset_id: int) -> None:
    """写入 fail_timeout 墓碑：timeout 失败后冷却 7 天，避免主流币反复超时。

    不覆盖已有的 ok / not_found 记录。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM biz.asset_token_unlocks "
            "WHERE asset_id = %s AND crawl_status IN ('ok', 'not_found')",
            (asset_id,),
        )
        if cur.fetchone():
            return
        cur.execute(
            """
            INSERT INTO biz.asset_token_unlocks (
                asset_id, source_url, source_name, slug,
                overview_json, unlock_events_json, revenue_json, valuation_json,
                scraped_at, updated_at, crawl_status, last_attempt_at
            ) VALUES (
                %s, NULL, 'unknown', NULL,
                '{}'::jsonb, '[]'::jsonb, '{}'::jsonb, '{}'::jsonb,
                NOW(), NOW(), 'fail_timeout', NOW()
            )
            ON CONFLICT (asset_id) DO UPDATE SET
                crawl_status = 'fail_timeout',
                last_attempt_at = NOW(),
                updated_at = NOW()
            """,
            (asset_id,),
        )
    conn.commit()
    print(f"    -> 已写入 fail_timeout 墓碑 (asset_id={asset_id})")


def run_single(asset_id: int, timeout: int = 60, allow_browser_search: bool = False) -> tuple[str, str]:
    """运行单币解锁采集，返回 (状态, 详情)。

    状态：ok / not_found / parse_empty / fail
    """
    script = SCRIPT_DIR / "phase_chain_token_unlocks.py"
    cmd = [
        sys.executable, "-u", str(script),
        "--asset-id", str(asset_id),
        "--save",
    ]
    if not allow_browser_search:
        cmd.append("--no-browser-search")  # 默认禁用浏览器首页搜索提速
    proc = None
    try:
        # start_new_session=True：超时后可用 killpg 清理整个进程组（含 Playwright chromium）
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(SCRIPT_DIR),
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            return "fail", "timeout"
        if proc.returncode != 0:
            return "fail", f"exit={proc.returncode}"

        # 解析 stdout 最后一行 JSON
        stdout_lines = [l for l in stdout.strip().split("\n") if l.strip()]
        if not stdout_lines:
            return "fail", "no_output"

        try:
            data = json.loads(stdout_lines[-1])
            status = data.get("status", "unknown")
            if status == "ok":
                events = len(data.get("unlock_events", []))
                # P1-3: overview 有信号但事件空 → parse_empty，视为疑似失败
                if data.get("crawl_status") == "parse_empty":
                    return "fail", "parse_empty"
                return "ok", f"events={events}"
            elif status == "not_found":
                return "not_found", "not_found"
            else:
                return "fail", f"status={status}"
        except (json.JSONDecodeError, ValueError):
            return "fail", "parse_error"

    except Exception as e:
        return "fail", f"error={e}"


def main():
    parser = argparse.ArgumentParser(description="代币解锁数据批量采集")
    parser.add_argument("--limit", type=int, default=0,
                        help="最多采集数量 (0=不限，全量)")
    parser.add_argument("--timeout", type=int, default=60,
                        help="单币超时时间（秒）")
    parser.add_argument("--delay", type=float, default=0.5,
                        help="每币之间延迟（秒）")
    parser.add_argument("--refresh-days", type=int, default=DEFAULT_REFRESH_DAYS,
                        help="同时刷新 crawl_status='ok' 且 updated_at 超 N 天的资产"
                             "（复验 #5；0=关闭，只抓新候选）。默认 %d。" % DEFAULT_REFRESH_DAYS)
    args = parser.parse_args()

    settings = get_settings(require_database=True)

    print("=" * 60)
    print("代币解锁数据批量采集")
    print("=" * 60)

    # get_connection 是 @contextmanager 生成器，必须用 with 才能拿到真实连接
    with get_connection(settings.database_url) as conn:
        total_new, total_refresh = get_total_pending(conn, args.refresh_days)
        total_pending = total_new + total_refresh
        limit = args.limit if args.limit > 0 else total_pending
        print(f"待采集总数: {total_pending}（新候选 {total_new} + 陈旧刷新 {total_refresh}"
              f"，刷新门槛 {args.refresh_days} 天），本次处理: {limit}")

        if limit == 0:
            print("无待采集资产，退出")
            return 0

        assets = get_pending_assets(conn, limit, args.refresh_days)
        if not assets:
            print("无待采集资产")
            return 0

    success = 0
    fail = 0
    not_found = 0
    t0 = time.time()

    for i, asset in enumerate(assets, 1):
        asset_id = asset["asset_id"]
        symbol = asset.get("symbol", "?")
        _tag = "[刷新] " if asset.get("is_refresh") else ""
        print(f"  [{i}/{len(assets)}] {_tag}asset_id={asset_id} {symbol} ... ",
              end="", flush=True)

        # P2-6: 每 200 个启用一次浏览器首页搜索兜底（提高 API 搜索被拦截时的命中率）。
        # 浏览器搜索走 Playwright chromium，耗时高且易挂死；从 i%50 降到 i%200 提速
        #（解锁早报依赖，需在 08:30 快照前跑完，且 09-14 曾因浏览器卡死 stuck 90 分钟）。
        allow_browser = (i % 200 == 0)
        status, info = run_single(asset_id, timeout=args.timeout,
                                   allow_browser_search=allow_browser)
        if status == "ok":
            success += 1
            print(f"OK ({info})")
        elif status == "not_found" or info == "timeout":
            # timeout 当 not_found 处理，避免 exit code 1
            not_found += 1
            print(f"NOT_FOUND ({info})")
            # timeout 写墓碑，7 天冷却避免反复超时
            if info == "timeout":
                try:
                    with get_connection(settings.database_url) as wconn:
                        _mark_fail_timeout(wconn, asset_id)
                except Exception as e:
                    print(f"    -> 写入 fail_timeout 失败: {e}")
        else:
            fail += 1
            print(f"FAIL ({info})")

        if i < len(assets) and args.delay > 0:
            time.sleep(args.delay)

        # 每 20 个打印一次进度摘要
        if i % 20 == 0:
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            eta = (len(assets) - i) / rate if rate > 0 else 0
            print(f"  -- 进度 {i}/{len(assets)} ({i/len(assets)*100:.1f}%), "
                  f"成功 {success}, not_found {not_found}, 失败 {fail}, "
                  f"速度 {rate*60:.1f}/h, 预计剩余 {eta/60:.1f}min --")

    elapsed = time.time() - t0
    print("\n" + "=" * 60)
    print(f"全部完成，耗时 {elapsed:.1f}s ({elapsed/60:.1f}min)")
    print(f"总计: 成功 {success}, not_found {not_found}, 失败 {fail}")
    print(f"平均速度: {len(assets)/elapsed*60:.1f} 币/小时" if elapsed > 0 else "")
    print("=" * 60)

    # P2-5: 失败率过高才返回 1（原逻辑 fail>0 即返回 1，导致任意单个币失败
    # 整个任务被标记 failed，连续多日制造调度噪音）。小比例失败属正常（反爬/超时）。
    fail_ratio = fail / max(len(assets), 1)
    return 1 if fail_ratio > 0.3 else 0


if __name__ == "__main__":
    sys.exit(main())
