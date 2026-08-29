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

# 数据截止日期
SOURCE_CUTOFF = date(2026, 8, 24)

# BTC asset_id（固定值，预期为 1）
BTC_ASSET_ID = 1

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
    """解析单个 CSV 文件，返回记录列表。"""
    records = []
    metric_name = file_path.stem  # 用文件名作为 metric_name

    with open(file_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # 解析日期
            date_str = row.get("date", "")
            if not date_str or len(date_str) < 10:
                continue

            metric_date = date_str[:10]  # 取 YYYY-MM-DD

            # 解析值
            value = safe_float(row.get("value", ""))

            # 解析其他字段
            unit = row.get("unit", "")
            frequency = row.get("frequency", "")
            release_version = row.get("release_version", "")

            records.append({
                "metric_name": metric_name,
                "metric_date": metric_date,
                "value": value,
                "unit": unit,
                "frequency": frequency,
                "release_version": release_version,
                "source_cutoff": SOURCE_CUTOFF,
            })

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

    print(f"\n{'='*50}")
    print(f"入库完成：{total_stats['files']} 个文件")
    print(f"总行数：{total_stats['total_rows']}")
    print(f"已入库：{total_stats['inserted']}")
    print(f"错误：{total_stats['errors']}")


if __name__ == "__main__":
    main()
