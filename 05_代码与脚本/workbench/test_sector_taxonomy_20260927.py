#!/usr/bin/env python3
"""工单 SECTOR-RECLASS-001 · launchpad 赛道（打新 / 发射台）离线回归护栏。

背景：CMC tag / CMC category_hint / CG / DL 的「launchpad」信号原先全部折进 defi，
      导致 PONS(11114) 等发射台资产被 DeFi 蓝筹做估值对标。

运行：python workbench/test_sector_taxonomy_20260927.py（纯离线，不连库、不连网）

覆盖：
  1  taxonomy 枚举 / 标签一致性（label 键集 == SECTORS 集合，无遗漏、无重复）
  2  CMC（tag / category_hint）、CG（launchpad / surge launchpad）、DL 三来源均产出 launchpad
  3  DL 大小写不敏感（"Launchpad"）
  4  不误伤真 DeFi：launchpad 低置信度 + defi 高置信度 → primary 仍取 defi
  5  PONS 再现：三来源真实信号 → primary = launchpad 且无 defi 残留
  6  nft launchpad 仍归 gamefi（本轮决策保留，见工单 §四 A2）
  7  落库侧：CHECK 白名单（create_asset_sector.sql + fix_071 迁移）含 launchpad
  8  SQL 版规则（生产每日流水线实际生效的 refresh_sectors_multi_source.sql）与 Python 版一致
  9  展示侧：index.html 两份硬编码表 + etl_sector_flow_daily.py 均含 launchpad
 10  边界留档：launchpad 未配置采集优先级 / 主题优先级（回退 other）
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))          # 05_代码与脚本/workbench
_ROOT = os.path.dirname(_HERE)                              # 05_代码与脚本
_SCRIPTS = os.path.join(_ROOT, "scripts")
sys.path.insert(0, os.path.join(_SCRIPTS, "src"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.mapping import sector as sc  # noqa: E402

passed = 0
failed = 0


def check(cond, name, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}" + (f"  → {detail}" if detail else ""))


def _read(path):
    try:
        return open(path, encoding="utf-8").read()
    except OSError as e:
        return f"__READ_ERROR__ {e}"


_SQL_REFRESH = _read(os.path.join(_SCRIPTS, "sql", "biz",
                                  "refresh_sectors_multi_source.sql"))
_SQL_CREATE = _read(os.path.join(_SCRIPTS, "sql", "biz", "create_asset_sector.sql"))
_SQL_MIGRATE = _read(os.path.join(_SCRIPTS, "migrations",
                                  "fix_071_launchpad_sector.sql"))
_HTML = _read(os.path.join(_HERE, "templates", "index.html"))
_ETL = _read(os.path.join(_SCRIPTS, "bin", "etl_sector_flow_daily.py"))

# SQL 版中「launchpad → launchpad 0.65」在 cg_hits / dl_hits 各一处
_SQL_LP_065 = "('launchpad', 'launchpad', 0.65)"


print("── 1. taxonomy 枚举 / 标签一致性 ──")
check("launchpad" in sc.SECTORS, "SECTORS 含 launchpad")
check(len(sc.SECTORS) == len(set(sc.SECTORS)), "SECTORS 无重复项",
      f"len={len(sc.SECTORS)} uniq={len(set(sc.SECTORS))}")
check(set(sc.SECTOR_LABELS) == set(sc.SECTORS), "SECTOR_LABELS 键集 == SECTORS 集合",
      f"仅在枚举: {sorted(set(sc.SECTORS) - set(sc.SECTOR_LABELS))}；"
      f"仅在标签: {sorted(set(sc.SECTOR_LABELS) - set(sc.SECTORS))}")
check(sc.SECTOR_LABELS.get("launchpad") == "Launchpad 打新平台",
      "SECTOR_LABELS['launchpad'] 文案正确", sc.SECTOR_LABELS.get("launchpad"))

print("\n── 2. 三来源 identify launchpad ──")
check(sc.classify_cmc_sectors(["launchpad"], None) == [("launchpad", 0.6)],
      "CMC tag launchpad → launchpad 0.6",
      sc.classify_cmc_sectors(["launchpad"], None))
check(sc.classify_cmc_sectors(None, "launchpad") == [("launchpad", 0.8)],
      "CMC category_hint launchpad → launchpad 0.8",
      sc.classify_cmc_sectors(None, "launchpad"))
check(sc.classify_cg_sectors(["launchpad"]) == [("launchpad", 0.65)],
      "CG launchpad → launchpad 0.65", sc.classify_cg_sectors(["launchpad"]))
check(sc.classify_cg_sectors(["surge launchpad"]) == [("launchpad", 0.5)],
      "CG surge launchpad → launchpad 0.5", sc.classify_cg_sectors(["surge launchpad"]))
check(sc.classify_dl_sectors("launchpad") == [("launchpad", 0.65)],
      "DL launchpad → launchpad 0.65", sc.classify_dl_sectors("launchpad"))

print("\n── 3. DL 大小写不敏感 ──")
check(sc.classify_dl_sectors("Launchpad") == [("launchpad", 0.65)],
      'DL "Launchpad"（DL 原始大小写）→ launchpad', sc.classify_dl_sectors("Launchpad"))

print("\n── 4. 不误伤真 DeFi ──")
_secs = sc.classify_cmc_sectors(["defi", "launchpad"], None)
check(sc.primary_sector(_secs) == "defi",
      "defi 0.9 + launchpad 0.6 → primary 仍为 defi", _secs)
check(sc.primary_sector(sc.merge_sectors([("launchpad", 0.65)], [("defi", 0.9)])) == "defi",
      "merge 后 launchpad 0.65 + defi 0.9 → primary 仍为 defi")

print("\n── 5. PONS(11114) 再现 ──")
_merged = sc.merge_sectors(
    sc.classify_cmc_sectors(["launchpad", "robinhood-ecosystem"], "launchpad"),
    sc.classify_cg_sectors(["Launchpad", "Robinhood Ecosystem", "Pons Launchpad"]),
    sc.classify_dl_sectors(None),
)
check(_merged == [("launchpad", 0.8)], "PONS 合并结果 == [(launchpad, 0.8)]", _merged)
check(sc.primary_sector(_merged) == "launchpad", "PONS primary == launchpad")
check(all(s != "defi" for s, _ in _merged), "PONS 无 defi 残留（干净翻档）", _merged)

print("\n── 6. nft launchpad 保留 gamefi（决策留档）──")
check(sc.classify_dl_sectors("nft launchpad") == [("gamefi", 0.6)],
      "DL nft launchpad 仍归 gamefi 0.6（NFT 属性更强）",
      sc.classify_dl_sectors("nft launchpad"))

print("\n── 7. CHECK 白名单 ──")
check("launchpad" in _SQL_CREATE, "create_asset_sector.sql CHECK 含 launchpad")
check("launchpad" in _SQL_MIGRATE, "fix_071_launchpad_sector.sql 含 launchpad")
check("DROP CONSTRAINT IF EXISTS chk_asset_sector_sector" in _SQL_MIGRATE
      and "ADD CONSTRAINT chk_asset_sector_sector" in _SQL_MIGRATE,
      "fix_071 为 DROP+ADD 幂等式")


def _check_whitelist(sql_text):
    """抽取 CHECK 白名单里的所有 'x' 字面量（取 IN ( ... ) 括号内）。"""
    import re
    m = re.search(r"sector IN \((.*?)\)", sql_text, re.S)
    return set(re.findall(r"'([a-z0-9_]+)'", m.group(1))) if m else set()


_wl_create = _check_whitelist(_SQL_CREATE)
_wl_migrate = _check_whitelist(_SQL_MIGRATE)
check(_wl_create == set(sc.SECTORS),
      "create_asset_sector.sql 白名单 == SECTORS（逐值一致、无遗漏/无多余）",
      f"差集 SQL-SECTORS={sorted(_wl_create - set(sc.SECTORS))} "
      f"SECTORS-SQL={sorted(set(sc.SECTORS) - _wl_create)}")
check(_wl_migrate == set(sc.SECTORS),
      "fix_071 白名单 == SECTORS（逐值一致）—— 防 stablecoin 之类后加枚举被漏掉",
      f"差集 SQL-SECTORS={sorted(_wl_migrate - set(sc.SECTORS))} "
      f"SECTORS-SQL={sorted(set(sc.SECTORS) - _wl_migrate)}")
check("stablecoin" in _wl_create and "stablecoin" in _wl_migrate,
      "两份 SQL 白名单均保留 stablecoin（生产现存 1046 行，漏掉会让 ADD CONSTRAINT 校验失败）")

print("\n── 8. SQL 版规则与 Python 版一致 ──")
check("('launchpad', 'launchpad', 0.6)" in _SQL_REFRESH,
      "SQL tag_hits: launchpad → launchpad 0.6")
check("('launchpad', 'launchpad', 0.8)" in _SQL_REFRESH,
      "SQL cat_hits: launchpad → launchpad 0.8")
check("('surge launchpad', 'launchpad', 0.5)" in _SQL_REFRESH,
      "SQL cg_hits: surge launchpad → launchpad 0.5")
check(_SQL_REFRESH.count(_SQL_LP_065) == 2,
      "SQL cg_hits + dl_hits: launchpad → launchpad 0.65（两处）",
      f"count={_SQL_REFRESH.count(_SQL_LP_065)}")
check("'launchpad', 'defi'" not in _SQL_REFRESH,
      "SQL 中已无 launchpad → defi 的残留映射")
check("('nft launchpad', 'gamefi', 0.6)" in _SQL_REFRESH,
      "SQL dl_hits nft launchpad 仍归 gamefi（与 Python 版一致）")

print("\n── 9. 展示侧 ──")
check("'launchpad': 'Launchpad 打新'" in _HTML, "index.html SECTOR_LABELS 含 launchpad")
check("'launchpad': 'Launchpad'" in _HTML, "index.html SECTOR_LABELS_SHORT 含 launchpad")
check(".sector-launchpad {" in _HTML, "index.html 补了 .sector-launchpad 配色")
check('"launchpad": "Launchpad"' in _ETL,
      "etl_sector_flow_daily.py SECTOR_LABELS 含 launchpad")

print("\n── 10. 边界留档 ──")
check("launchpad" not in sc.SECTOR_COLLECT_PRIORITY
      and sc.get_sector_collect_priority("launchpad")
      == sc.SECTOR_COLLECT_PRIORITY["other"],
      "采集优先级未配置 launchpad，回退 other（工单边界项）")
check("launchpad" not in sc.SECTOR_SCORE_WEIGHTS
      and sc.get_sector_weights("launchpad") == sc.DEFAULT_SCORE_WEIGHTS,
      "评分权重未配置 launchpad，回退默认权重（工单边界项）")

print(f"\n{'=' * 60}\nPASS {passed} / FAIL {failed}")
raise SystemExit(1 if failed else 0)