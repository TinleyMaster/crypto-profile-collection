"""入库脚本：Open Bitcoin Metrics BTC 链上日频指标（长表）。

从 data_external/obm/*.csv 读取本地文件，解析后 upsert 到 biz.obm_btc_daily。

用法：
    python ingest_obm_btc_daily.py                     # 入库所有 CSV
    python ingest_obm_btc_daily.py --dry-run            # 预览，不写入
    python ingest_obm_btc_daily.py --data-dir /path     # 指定数据目录
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

# BTC asset_id（固定值，预期为 1）
BTC_ASSET_ID = 1

# 数据截止日期：动态取各 CSV 的最大 metric_date（不再硬编码，避免 source_cutoff 语义失真）

# UPSERT SQL
UPSERT_SQL = """
INSERT INTO biz.obm_btc_daily (
    metric_name, metric_date, value, unit, frequency, release_version, source_cutoff
) VALUES (
    %(metric_name)s, %(metric_date)s, %(value)s, %(unit)s, %(frequency)s, %(release_version)s, %(source_cutoff)s
)
ON CONFLICT (metric_name, metric_date) DO UPDATE SET
    value = EXCLUDED.value,
    unit = EXCLUDED.unit,
    frequency = EXCLUDED.frequency,
    release_version = EXCLUDED.release_version
"""


def safe_float(v: str) -> float | None:
    """安全转换为 float，空值/NaN 返回 None。"""
    if v in ("", "null", "NaN", "None"):
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def parse_csv_file(file_path: Path) -> list[dict]:
    """解析单个 CSV 文件，返回记录列表。

    支持两种格式：
    1. 单 value 列（标准长表）→ 直接读取
    2. 多列宽表（如 age-band：1d/1w/1m/3m/6m/1yr 等）→ unpivot 为多条记录
       metric_name = `{文件名}_{列名}`（如 obm_cdd_age_band_btcxdays_daily_1d）
    """
    records = []
    metric_name = file_path.stem  # 用文件名作为 metric_name

    with open(file_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return records

        # 判断是否有 value 列
        has_value_col = "value" in reader.fieldnames

        # 多列宽表：排除非数据列，剩余为 age-band 列
        skip_cols = {"date", "value", "unit", "frequency", "release_version"}
        data_cols = [c for c in reader.fieldnames if c not in skip_cols]

        for row in reader:
            # 解析日期
            date_str = row.get("date", "")
            if not date_str or len(date_str) < 10:
                continue
            metric_date = date_str[:10]

            unit = row.get("unit", "")
            frequency = row.get("frequency", "")
            release_version = row.get("release_version", "")

            if has_value_col:
                # 标准单 value 列
                value = safe_float(row.get("value", ""))
                records.append({
                    "metric_name": metric_name,
                    "metric_date": metric_date,
                    "value": value,
                    "unit": unit,
                    "frequency": frequency,
                    "release_version": release_version,
                })
            elif data_cols:
                # 多列宽表 → unpivot
                for col in data_cols:
                    value = safe_float(row.get(col, ""))
                    sub_name = f"{metric_name}_{col}"
                    records.append({
                        "metric_name": sub_name,
                        "metric_date": metric_date,
                        "value": value,
                        "unit": unit,
                        "frequency": frequency,
                        "release_version": release_version,
                    })

    # 动态 source_cutoff：取本 CSV 最大 metric_date（真实数据截止日，避免硬编码误导）
    if records:
        max_date = max(r["metric_date"] for r in records)
        for r in records:
            r["source_cutoff"] = max_date

    return records


def ingest_file(conn, file_path: Path, dry_run: bool) -> dict:
    """入库单个 CSV 文件。返回统计信息。"""
    stats = {
        "file": file_path.name,
        "total_rows": 0,
        "inserted": 0,
        "skipped_no_date": 0,
    }

    records = parse_csv_file(file_path)
    stats["total_rows"] = len(records)

    if not records:
        return stats

    # 过滤无效记录
    valid_records = [r for r in records if r["metric_date"]]
    stats["skipped_no_date"] = stats["total_rows"] - len(valid_records)

    if dry_run:
        stats["inserted"] = len(valid_records)
        return stats

    # 批量 upsert
    if valid_records:
        with conn.cursor() as cur:
            cur.executemany(UPSERT_SQL, valid_records)
        stats["inserted"] = len(valid_records)

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="入库 OBM BTC 链上日频指标")
    parser.add_argument("--data-dir", type=str, default=None, help="数据目录路径")
    parser.add_argument("--dry-run", action="store_true", help="预览，不写入数据库")
    args = parser.parse_args()

    # 确定数据目录
    if args.data_dir:
        data_dir = Path(args.data_dir)
    else:
        # 判断 Docker 环境
        if os.path.exists("/app/scripts/bin"):
            data_dir = Path("/app/data_external/obm")
        else:
            # 本地环境：项目根目录/data_external/obm/
            project_root = SCRIPT_DIR.parent.parent.parent
            data_dir = project_root / "data_external" / "obm"

    if not data_dir.exists():
        print(f"错误：数据目录不存在 {data_dir}", file=sys.stderr)
        sys.exit(1)

    # 获取所有 CSV 文件
    csv_files = sorted(data_dir.glob("*.csv"))
    if not csv_files:
        print(f"错误：目录中无 CSV 文件 {data_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"找到 {len(csv_files)} 个 CSV 文件")
    if args.dry_run:
        print("[DRY-RUN] 模式，不写入数据库")

    settings = get_settings(require_database=True)
    total_stats = {"files": 0, "total_rows": 0, "inserted": 0, "errors": 0}

    with get_connection(settings.database_url) as conn:
        for i, csv_file in enumerate(csv_files, 1):
            print(f"\n[{i}/{len(csv_files)}] {csv_file.name}...")
            try:
                stats = ingest_file(conn, csv_file, args.dry_run)
                total_stats["files"] += 1
                total_stats["total_rows"] += stats["total_rows"]
                total_stats["inserted"] += stats["inserted"]
                print(f"  {stats['total_rows']} 行 → {stats['inserted']} 行入库")
            except Exception as e:
                print(f"  [ERROR] {csv_file.name}: {e}", file=sys.stderr)
                total_stats["errors"] += 1

        # 上游新鲜度校验：max(metric_date) 明显落后当日 → 告警（不失败），task 日志可见
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT MAX(metric_date) FROM biz.obm_btc_daily")
                row = cur.fetchone()
                max_date = row[0] if row else None
            if max_date is not None:
                stale_days = (date.today() - max_date).days
                if stale_days > 3:
                    print(
                        f"\n⚠️ [STALE] OBM 数据停留 {max_date}（滞后 {stale_days} 天）"
                        f"，上游未更新或下载失败，请关注 OBM 源",
                        file=sys.stderr,
                    )
                else:
                    print(f"\n✓ 数据新鲜度 OK：最新 {max_date}（滞后 {stale_days} 天）")
            else:
                print("\n⚠️ [STALE] OBM 表无数据", file=sys.stderr)
        except Exception as e:
            print(f"\n⚠️ 新鲜度校验失败：{e}", file=sys.stderr)

    print(f"\n{'='*50}")
    print(f"入库完成：{total_stats['files']} 个文件")
    print(f"总行数：{total_stats['total_rows']}")
    print(f"已入库：{total_stats['inserted']}")
    print(f"错误：{total_stats['errors']}")


if __name__ == "__main__":
    main()
