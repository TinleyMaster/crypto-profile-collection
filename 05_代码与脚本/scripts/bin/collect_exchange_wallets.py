"""
交易所钱包地址自动收集脚本（工单：大盘_基础_CEX地址自动收集）。

目标：自动采集 + 保守入库，把 CEX 地址规模从"手填少量"扩到"跨交易所 + 跨主流链"，
且新地址经分级校验后才进计算。不改动表结构、不改动现有读取逻辑，只增不删。

采集路径（首版）：
  A. 社区公开地址库（auto_community）—— GitHub 仓库，多格式解析（JSON map / JSON list / CSV / 纯文本）
  B. 持仓快照标签反查（auto_ethplorer）—— 复用 biz.onchain_holder_snapshot.top_holders_json 里
     命中交易所关键词的 holder 标签（零边际成本，顺手收）
  C. Dune 图聚类（auto_dune_graph）—— 二期，本脚本预留开关，默认关闭（耗 credits + 需人工确认）

分级入库规则（防误标核心）：
  high  : >=2 个独立源互证同一地址 = 同一交易所   → 直接参与净流计算
  medium: 单源命中 + 地址格式/链匹配              → 进表但默认不参与净流计算，待二次确认升 high
  low   : 单源命中但无交叉验证或格式异常          → 仅记录（写审计文件），永不进计算

用法：
  python collect_exchange_wallets.py                       # dry-run：只输出新增候选 diff，不写库
  python collect_exchange_wallets.py --apply               # 写入 biz.onchain_exchange_wallet
  python collect_exchange_wallets.py --chains eth,bsc      # 限制链
  python collect_exchange_wallets.py --sources community,ethplorer   # 限制路径
  python collect_exchange_wallets.py --verify-high eth     # 打印某链 high 地址集合（净流计算验证）

网络：requests 自动读 HTTP_PROXY/HTTPS_PROXY；可用 --proxy 显式指定。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import requests
import psycopg
import psycopg.rows

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

# ── 配置 ──────────────────────────────────────────────────

# 社区源 A 配置（可扩展）。missing_ok=True 表示仓库不存在时仅 ⚠️ 缺失标注，不中断主流程。
# fmt:
#   json_map     {address: exchange_name}
#   json_list    [{address:..., exchange/name:...}, ...]
#   csv          address,exchange_name（首行可含表头）
#   plain       每行 "address exchange_name"（空格/逗号分隔）；或仅 address（用 file_exchange 兜底）
COMMUNITY_SOURCES: list[dict[str, Any]] = [
    {
        "name": "tradezon/cex-list",
        "url": "https://raw.githubusercontent.com/tradezon/cex-list/main/data/ethereum-mainnet.json",
        "tar_url": "https://codeload.github.com/tradezon/cex-list/tar.gz/refs/heads/main",
        "tar_path": "cex-list-main/data/ethereum-mainnet.json",
        "chain": "eth",
        "fmt": "json_map",
        "missing_ok": False,
        "note": "社区维护的 CEX 地址清单（JSON map），仅 ETH；已实测可用（2026-08-27）。",
    },
    {
        # 工单默认首选仓库 cloudac7/cex-wallet-addresses —— 已实测 404 不存在（2026-08-27）。
        # 保留为配置项，拉到即 ⚠️ 缺失标注；若日后恢复可用可去掉 missing_ok。
        "name": "cloudac7/cex-wallet-addresses",
        "url": "https://raw.githubusercontent.com/cloudac7/cex-wallet-addresses/main/README.md",
        "chain": "eth",
        "fmt": "plain",
        "missing_ok": True,
        "note": "工单默认仓库，已实测 404；仅作配置占位，采集失败显式缺失标注。",
    },
]

# 交易所名称归一化（社区小写 / 快照标签 → 规范显示名）
EXCHANGE_NAME_MAP = {
    "binance": "Binance",
    "binance us": "Binance US",
    "binanceus": "Binance US",
    "coinbase": "Coinbase",
    "coinbase prime": "Coinbase Prime",
    "okx": "OKX",
    "okex": "OKX",
    "kraken": "Kraken",
    "bybit": "Bybit",
    "kucoin": "KuCoin",
    "gate": "Gate.io",
    "gate.io": "Gate.io",
    "huobi": "Huobi",
    "htx": "HTX",
    "bitfinex": "Bitfinex",
    "bitget": "Bitget",
    "mexc": "MEXC",
    "crypto.com": "Crypto.com",
    "cryptocom": "Crypto.com",
    "upbit": "Upbit",
    "bithumb": "Bithumb",
    "gemini": "Gemini",
    "bitstamp": "Bitstamp",
    "poloniex": "Poloniex",
    "deribit": "Deribit",
    "bitmart": "BitMart",
    "lbank": "LBank",
    "xt.com": "XT.COM",
    "xtcom": "XT.COM",
    "bittrex": "Bittrex",
    "bitmex": "BitMEX",
    "korbit": "Korbit",
    "coinone": "Coinone",
    "coinbit": "Coinbit",
    "ftx": "FTX",
    "hotbit": "Hotbit",
    "gateio": "Gate.io",
}

# 快照标签 → 交易所关键词（Path B 反查用）。按长度降序匹配，避免 "gate" 误命中 "aggregate"。
EXCHANGE_KEYWORDS = sorted(
    (
        "binance", "binanceus", "coinbase", "coinbaseprime", "okx", "kraken", "bybit",
        "kucoin", "gate.io", "huobi", "htx", "bitfinex", "bitget", "mexc",
        "crypto.com", "cryptocom", "upbit", "bithumb", "gemini", "bitstamp",
        "poloniex", "deribit", "bitmart", "lbank", "xt.com", "bittrex", "bitmex",
        "korbit", "coinone", "coinbit", "ftx", "hotbit",
    ),
    key=len,
    reverse=True,
)

# 社区源里出现的"非交易所"标签黑名单（如链名 cronos、mexc 变体等），命中则丢弃。
# 防误标：社区库质量参差，个别标签指向链/协议而非 CEX，绝不入库。
NON_EXCHANGE_LABELS = frozenset({
    "cronos",  # Cronos 链 treasury，非交易所
    "ftx us",  # 若社区给到 FTX US 汇总标签，仍需人工核；默认不自动入 high
})

# 快照表链名 → 交易所钱包表链名
CHAIN_ALIASES = {
    "ethereum": "eth",
    "eth": "eth",
    "bsc": "bsc",
    "bnb": "bsc",
    "arbitrum": "arbitrum",
    "arb": "arbitrum",
    "base": "base",
    "optimism": "optimism",
    "op": "optimism",
    "avalanche": "avalanche",
    "avax": "avalanche",
    "polygon": "polygon",
    "matic": "polygon",
    "solana": "solana",
    "sol": "solana",
    "tron": "tron",
    "trx": "tron",
}

# 大小写敏感链（非 EVM，地址不得 lower()，否则指向错误地址）
CASE_SENSITIVE_CHAINS = frozenset({"solana", "tron", "ton", "sui", "aptos"})

RE_EVM = re.compile(r"^0x[a-fA-F0-9]{40}$")
RE_SOLANA = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
RE_TRON = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")

# Path B 数据源标识
SOURCE_COMMUNITY = "auto_community"
SOURCE_ETHPLORER = "auto_ethplorer"
SOURCE_DUNE = "auto_dune_graph"

DEFAULT_HTTP_TIMEOUT = 30


# ── 工具函数 ──────────────────────────────────────────────

def _norm_chain(raw: str) -> str:
    """链名归一化：ethereum→eth, sol→solana 等。"""
    return CHAIN_ALIASES.get((raw or "").strip().lower(), (raw or "").strip().lower())


def _norm_address(chain: str, addr: str) -> str:
    """地址归一化：EVM 小写；非 EVM（solana/tron）大小写敏感保持原样。"""
    a = (addr or "").strip()
    if not a:
        return ""
    if chain in CASE_SENSITIVE_CHAINS:
        return a
    return a.lower()


def _is_valid_address(chain: str, addr: str) -> bool:
    """校验地址格式是否匹配链。格式不合法一律进 low，绝不进计算。"""
    if not addr:
        return False
    if chain in ("solana", "sol"):
        return bool(RE_SOLANA.match(addr))
    if chain in ("tron", "trx"):
        return bool(RE_TRON.match(addr))
    return bool(RE_EVM.match(addr))


def _norm_exchange(raw: str) -> str:
    """交易所名称归一化：小写映射到规范名；未识别时保留原样去空格。"""
    if not raw:
        return ""
    key = raw.strip().lower().replace(" ", "")
    mapped = EXCHANGE_NAME_MAP.get(key) or EXCHANGE_NAME_MAP.get(raw.strip().lower())
    if mapped:
        return mapped
    return raw.strip()


def _fetch(url: str, timeout: int = DEFAULT_HTTP_TIMEOUT, proxy: str | None = None) -> str | None:
    """GET 请求，带重试 + 指数退避。失败返回 None。"""
    raw = _fetch_bytes(url, timeout=timeout, proxy=proxy)
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="replace")


def _fetch_bytes(url: str, timeout: int = DEFAULT_HTTP_TIMEOUT,
                 proxy: str | None = None) -> bytes | None:
    """GET 请求返回原始 bytes（用于 tarball），带重试 + 指数退避。"""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; cex-addr-collector/1.0)"}
    proxies = None
    if proxy:
        proxies = {"http": proxy, "https": proxy}
    last_err = None
    for attempt in range(5):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout, proxies=proxies)
            resp.raise_for_status()
            return resp.content
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < 4:
                time.sleep(2 ** attempt + 1)
    print(f"  [WARN] 请求失败 {url}: {str(last_err)[:100]}")
    return None


# ── 路径 A：社区源解析 ───────────────────────────────────

def _parse_candidate(chain: str, addr: str, exchange: str, src_key: str) -> dict | None:
    """构造单条候选（若地址/交易所非法则返回 None）。"""
    chain = _norm_chain(chain)
    addr = _norm_address(chain, addr)
    exchange = _norm_exchange(exchange)
    if not addr or not exchange:
        return None
    if exchange.lower().replace(" ", "") in NON_EXCHANGE_LABELS:
        return None
    if not _is_valid_address(chain, addr):
        return {
            "address": addr, "exchange_name": exchange, "chain": chain,
            "confidence": "low", "source": src_key,
            "sources": [src_key], "hit_reason": f"地址格式不匹配链 {chain}，判定 low",
        }
    return {
        "address": addr, "exchange_name": exchange, "chain": chain,
        "confidence": "low", "source": src_key,
        "sources": [src_key], "hit_reason": "",
    }


def _parse_json_map(text: str, chain: str, src_key: str) -> list[dict]:
    out = []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return out
    if not isinstance(data, dict):
        return out
    for addr, ex in data.items():
        c = _parse_candidate(chain, addr, str(ex or ""), src_key)
        if c:
            out.append(c)
    return out


def _parse_json_list(text: str, chain: str, src_key: str) -> list[dict]:
    out = []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return out
    if not isinstance(data, list):
        return out
    for item in data:
        if not isinstance(item, dict):
            continue
        addr = item.get("address") or item.get("addr") or item.get("wallet") or ""
        ex = item.get("exchange") or item.get("name") or item.get("label") or ""
        c = _parse_candidate(chain, addr, str(ex or ""), src_key)
        if c:
            out.append(c)
    return out


def _parse_csv(text: str, chain: str, src_key: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in re.split(r"[,\t;]", line)]
        if len(parts) < 2:
            continue
        addr, ex = parts[0], parts[-1]
        if addr.lower().startswith("address"):
            continue  # 表头
        c = _parse_candidate(chain, addr, ex, src_key)
        if c:
            out.append(c)
    return out


def _parse_plain(text: str, chain: str, src_key: str, file_exchange: str = "") -> list[dict]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in re.split(r"[,\t; ]+", line) if p.strip()]
        addr = parts[0]
        ex = parts[1] if len(parts) >= 2 else file_exchange
        c = _parse_candidate(chain, addr, ex, src_key)
        if c:
            out.append(c)
    return out


def _fetch_community_source(cfg: dict[str, Any], proxy: str | None) -> list[dict]:
    """拉取单个社区源并解析为标准候选列表。失败时返回空 + 打印缺失标注。"""
    name = cfg.get("name", "?")
    url = cfg.get("url", "")
    chain = cfg.get("chain", "eth")
    fmt = cfg.get("fmt", "plain")
    src_key = f"{SOURCE_COMMUNITY}:{name}"
    print(f"\n  [社区源] {name} (chain={chain}, fmt={fmt})")

    text = _fetch(url, proxy=proxy) if url else None
    # raw.githubusercontent 偶发不稳定，回退 codeload tarball（仅 raw 失败时）
    if not text and cfg.get("tar_url"):
        print(f"  [INFO] raw 拉取失败，尝试 tarball 回退: {cfg['tar_url']}")
        raw = _fetch_bytes(cfg["tar_url"], proxy=proxy)
        if raw:
            text = _extract_from_tarball(raw, cfg.get("tar_path", ""))

    if not text:
        if cfg.get("missing_ok"):
            print(f"  [WARN] ⚠️ 缺失标注：社区源 {name} 不可用/不存在（已按 missing_ok 跳过，不中断）")
        else:
            print(f"  [WARN] 社区源 {name} 拉取失败，跳过")
        return []

    if fmt == "json_map":
        candidates = _parse_json_map(text, chain, src_key)
    elif fmt == "json_list":
        candidates = _parse_json_list(text, chain, src_key)
    elif fmt == "csv":
        candidates = _parse_csv(text, chain, src_key)
    else:
        candidates = _parse_plain(text, chain, src_key, file_exchange=cfg.get("file_exchange", ""))
    print(f"  [OK] {name}: 解析到 {len(candidates)} 条候选")
    return candidates


def _extract_from_tarball(data: bytes, tar_path: str) -> str | None:
    """从 codeload tarball（gzip bytes）提取指定文件内容。"""
    import io
    import tarfile

    try:
        tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
        for m in tf.getmembers():
            if m.isfile() and m.name.endswith(tar_path):
                f = tf.extractfile(m)
                return f.read().decode("utf-8", errors="replace") if f else None
        # 未精确命中 tar_path 时，返回同名 basename 的文件
        for m in tf.getmembers():
            if m.isfile() and m.name.split("/")[-1] == tar_path.split("/")[-1]:
                f = tf.extractfile(m)
                return f.read().decode("utf-8", errors="replace") if f else None
    except Exception as e:  # noqa: BLE001
        print(f"  [WARN] tarball 解析失败: {str(e)[:100]}")
    return None


def collect_community(proxy: str | None, chains: set[str]) -> list[dict]:
    """路径 A：拉取全部社区源。"""
    candidates: list[dict] = []
    for cfg in COMMUNITY_SOURCES:
        c = _fetch_community_source(cfg, proxy)
        candidates.extend(x for x in c if x["chain"] in chains)
    return candidates


# ── 路径 B：持仓快照标签反查 ─────────────────────────────

def _match_exchange_from_label(label: str) -> str:
    """从快照标签提取交易所名。例：'Binance 10'→'Binance'，'OKX: Hot Wallet 5'→'OKX'。"""
    if not label:
        return ""
    low = label.lower()
    for kw in EXCHANGE_KEYWORDS:
        if kw in low:
            ex = _norm_exchange(kw)
            if ex.lower().replace(" ", "") in NON_EXCHANGE_LABELS:
                return ""
            return ex
    return ""


def collect_from_snapshot_labels(conn, chains: set[str]) -> list[dict]:
    """路径 B：扫描 biz.onchain_holder_snapshot 的 top_holders_json 标签，命中交易所关键词即收为候选。

    零边际成本：直接复用每日持仓快照已采集的标签数据（区块浏览器/Explorer 标签）。
    """
    print("\n  [快照标签反查] 扫描 biz.onchain_holder_snapshot.top_holders_json ...")
    candidates: list[dict] = []
    src_key = SOURCE_ETHPLORER

    # SQL 侧过滤：只返回带非空 label 的 holder，避免全量回传大 JSON
    chain_sql = ",".join(["%s"] * len(chains))
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(f"""
            SELECT s.chain, h->>'address' AS address, h->>'label' AS label
            FROM biz.onchain_holder_snapshot s,
                 LATERAL jsonb_array_elements(s.top_holders_json) h
            WHERE jsonb_typeof(s.top_holders_json) = 'array'
              AND s.chain IN ({chain_sql})
              AND h->>'label' IS NOT NULL
              AND h->>'label' <> ''
        """, tuple(chains))
        rows = cur.fetchall()

    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        chain = _norm_chain(row["chain"])
        if chain not in chains:
            continue
        label = row.get("label") or ""
        ex = _match_exchange_from_label(label)
        if not ex:
            continue
        addr = _norm_address(chain, row.get("address") or "")
        if not addr:
            continue
        if not _is_valid_address(chain, addr):
            candidates.append({
                "address": addr, "exchange_name": ex, "chain": chain,
                "confidence": "low", "source": src_key,
                "sources": [src_key], "hit_reason": f"标签 '{label}' 命中 {ex}，但地址格式不匹配链 {chain}",
            })
            continue
        key = (addr, chain, ex)
        if key in seen:
            continue
        seen.add(key)
        candidates.append({
            "address": addr, "exchange_name": ex, "chain": chain,
            "confidence": "medium", "source": src_key,
            "sources": [src_key], "hit_reason": f"持仓快照标签 '{label}' 命中交易所关键词",
        })

    print(f"  [OK] 快照标签反查: {len(candidates)} 条候选")
    return candidates


# ── 分级判定 ─────────────────────────────────────────────

def _merge_candidates(candidates: list[dict]) -> list[dict]:
    """按 (address, chain) 合并多源候选，判定 confidence。

    规则（§5）：
      - >=2 个独立源且交易所归属一致 → high（直接进计算）
      - 单源 + 地址格式合法 → medium（进表不参与净流计算）
      - 单源 + 格式异常/无交叉验证 → low（仅记录）
      - 多源但交易所归属冲突 → low（保守：归属冲突视为不可信，仅记录）
    """
    merged: dict[tuple[str, str], dict] = {}
    for c in candidates:
        key = (c["address"], c["chain"])
        if key not in merged:
            merged[key] = {
                "address": c["address"], "exchange_name": c["exchange_name"],
                "chain": c["chain"], "label": "exchange",
                "sources": list(c.get("sources", [])),
                "hit_reason": c.get("hit_reason", ""),
            }
            continue
        m = merged[key]
        # 合并独立源（去重）
        for s in c.get("sources", []):
            if s not in m["sources"]:
                m["sources"].append(s)
        if m["exchange_name"] != c["exchange_name"]:
            # 归属冲突：记录冲突，最终按 low 处理
            m["_conflict"] = (m["exchange_name"], c["exchange_name"])
        # hit_reason 拼接
        if c.get("hit_reason") and c.get("hit_reason") not in m["hit_reason"]:
            m["hit_reason"] = (m["hit_reason"] + "；" + c["hit_reason"]).strip("；")

    result: list[dict] = []
    for m in merged.values():
        n_src = len(m["sources"])
        conflict = m.pop("_conflict", None)
        format_bad = "地址格式" in (m.get("hit_reason") or "")
        # source 列存"源族"名（§5：auto_community / auto_ethplorer），具体源在 sources 里
        source_family = m["sources"][0].split(":")[0] if m["sources"] else "auto"
        if conflict:
            m["confidence"] = "low"
            m["source"] = source_family
            m["hit_reason"] = f"多源归属冲突({conflict[0]} vs {conflict[1]})，保守降 low"
        elif n_src >= 2 and not format_bad:
            m["confidence"] = "high"
            m["source"] = source_family
            m["hit_reason"] = (m["hit_reason"] or "") + f"；{n_src} 个独立源互证"
        elif format_bad:
            m["confidence"] = "low"
            m["source"] = source_family
            m["hit_reason"] = (m["hit_reason"] or "") + "；地址格式异常，仅记录"
        else:
            m["confidence"] = "medium"
            m["source"] = source_family
        result.append(m)
    return result


# ── 去重 diff + 写入 ─────────────────────────────────────

def _load_existing(conn) -> tuple[set[tuple[str, str]], dict[tuple[str, str], str]]:
    """读取表内现有 (address, chain)（EVM 统一小写比对）与 exchange_name 映射。"""
    existing: set[tuple[str, str]] = set()
    name_map: dict[tuple[str, str], str] = {}
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("SELECT address, exchange_name, chain FROM biz.onchain_exchange_wallet")
        for r in cur.fetchall():
            chain = r["chain"]
            addr = _norm_address(chain, r["address"])
            key = (addr, chain)
            existing.add(key)
            name_map.setdefault(key, r["exchange_name"])
    return existing, name_map


def _diff_new(candidates: list[dict], existing: set[tuple[str, str]],
              name_map: dict[tuple[str, str], str]) -> tuple[list[dict], list[dict]]:
    """返回 (新增候选, 已存在跳过候选)。已存在但交易所归属不同的不覆盖，仅记录提示。"""
    new: list[dict] = []
    skipped: list[dict] = []
    for c in candidates:
        key = (c["address"], c["chain"])
        if key in existing:
            prev = name_map.get(key, "")
            if prev and prev != c["exchange_name"]:
                print(f"  [SKIP] {c['address']}@{c['chain']} 已存在(归属 {prev})，新候选归属 {c['exchange_name']} 冲突，跳过不覆盖")
            skipped.append(c)
            continue
        new.append(c)
    return new, skipped


def _apply_insert(conn, new_candidates: list[dict]) -> int:
    """INSERT 新地址（仅 high/medium 进主表；low 仅记录，不进计算表）。"""
    inserted = 0
    with conn.cursor() as cur:
        for c in new_candidates:
            if c["confidence"] not in ("high", "medium"):
                continue
            try:
                cur.execute("""
                    INSERT INTO biz.onchain_exchange_wallet
                        (address, exchange_name, chain, label, confidence, source)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (address, chain) DO NOTHING
                """, (
                    c["address"], c["exchange_name"], c["chain"],
                    c.get("label", "exchange"), c["confidence"], c["source"],
                ))
                if cur.rowcount:
                    inserted += 1
            except Exception as e:  # noqa: BLE001
                print(f"  [WARN] 插入失败 {c['address']}@{c['chain']}: {str(e)[:100]}")
    conn.commit()
    return inserted


# ── 净流计算读取 helper（P1-3 复用） ─────────────────────

def get_exchange_addresses(conn, chain: str, min_confidence: str = "high") -> set[str]:
    """按链返回参与净流计算的交易所地址集合（默认仅 high）。

    供 P1-3 净流自算（Dune 免费档 SQL）复用；medium/low 默认被排除，
    如需放行可传 min_confidence='medium'。这是"medium/low 不参与净流计算"的开关。
    """
    chain = _norm_chain(chain)
    confs = {"high", "medium", "low"}
    allowed = [c for c in confs if _conf_level(c) <= _conf_level(min_confidence)]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT address FROM biz.onchain_exchange_wallet WHERE chain = %s AND confidence = ANY(%s)",
            (chain, allowed),
        )
        return {_norm_address(chain, r[0]) for r in cur.fetchall()}


def _conf_level(c: str) -> int:
    return {"high": 0, "medium": 1, "low": 2}.get(c, 2)


# ── 主流程 ────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="CEX 地址自动收集（路径A社区源 + 路径B快照标签反查）")
    p.add_argument("--apply", action="store_true", help="写入数据库（默认 dry-run 只输出 diff）")
    p.add_argument("--chains", type=str, default="eth,bsc",
                   help="目标链，逗号分隔（默认 eth,bsc；非 EVM 二期）")
    p.add_argument("--sources", type=str, default="community,ethplorer",
                   help="采集路径，逗号分隔：community / ethplorer（dune 二期预留）")
    p.add_argument("--proxy", type=str, default=None,
                   help="HTTP 代理，如 http://127.0.0.1:7890（缺省读环境变量）")
    p.add_argument("--verify-high", type=str, default=None,
                   help="仅打印指定链的 high 地址集合（供净流计算验证），不采集")
    p.add_argument("--audit-out", type=str, default=None,
                   help="low 候选审计输出文件（JSON），默认仅打印")
    return p


def main() -> int:
    args = build_parser().parse_args()

    # verify-high 模式：只读，验证 high 地址能被正确读取
    if args.verify_high:
        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            addrs = get_exchange_addresses(conn, args.verify_high, min_confidence="high")
        print(json.dumps({
            "status": "ok", "chain": args.verify_high, "count": len(addrs),
            "addresses": sorted(addrs),
        }, ensure_ascii=False, default=str))
        return 0

    chains = {_norm_chain(c.strip()) for c in args.chains.split(",") if c.strip()}
    sources = {s.strip().lower() for s in args.sources.split(",") if s.strip()}

    settings = get_settings(require_database=True)
    proxy = args.proxy or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")

    candidates: list[dict] = []
    source_flags: list[str] = []
    if "community" in sources:
        source_flags.append("A(社区源)")
        candidates.extend(collect_community(proxy, chains))
    if "ethplorer" in sources:
        source_flags.append("B(快照标签反查)")
        with get_connection(settings.database_url) as conn:
            candidates.extend(collect_from_snapshot_labels(conn, chains))
    if "dune" in sources:
        print("  [WARN] 路径C(Dune 图聚类)为二期能力，本脚本未实现，忽略")
    if not candidates:
        print(json.dumps({"status": "ok", "message": "无候选", "candidates": []}, ensure_ascii=False))
        return 0

    print(f"\n===== 分级判定（合并 {len(candidates)} 条候选） =====")
    merged = _merge_candidates(candidates)
    stats: dict[str, int] = {"high": 0, "medium": 0, "low": 0}
    for m in merged:
        stats[m["confidence"]] = stats.get(m["confidence"], 0) + 1
    print(f"  候选合计: {len(merged)}（high={stats.get('high',0)}, medium={stats.get('medium',0)}, low={stats.get('low',0)}）")

    with get_connection(settings.database_url) as conn:
        existing, name_map = _load_existing(conn)
        print(f"  表内已有: {len(existing)} 条")
        new, skipped = _diff_new(merged, existing, name_map)
        print(f"  新增候选: {len(new)}（已存在跳过: {len(skipped)}）")

        if not args.apply:
            print("\n========== [DRY-RUN] 新增候选 diff（未写库） ==========")
            for c in new:
                print(json.dumps({
                    "address": c["address"], "exchange_name": c["exchange_name"],
                    "chain": c["chain"], "confidence": c["confidence"],
                    "source": c["source"], "sources": c.get("sources", []),
                    "hit_reason": c.get("hit_reason", ""),
                }, ensure_ascii=False, default=str))
            print(f"\n[DRY-RUN] 将新增 {len(new)} 条（high/medium 进表，low 仅记录）；加 --apply 写入")

            # low 审计输出
            low_cands = [c for c in new if c["confidence"] == "low"]
            if low_cands:
                out_path = args.audit_out
                if out_path:
                    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
                    Path(out_path).write_text(
                        json.dumps(low_cands, ensure_ascii=False, indent=2), encoding="utf-8")
                    print(f"  low 候选已记录到 {out_path}")
                else:
                    print("  [INFO] low 候选（仅记录）:")
                    for c in low_cands:
                        print(json.dumps(c, ensure_ascii=False, default=str))

            print(json.dumps({
                "status": "ok", "dry_run": True, "candidate_total": len(merged),
                "new_candidates": len(new),
                "stats": stats, "chains": sorted(chains),
                "sources": source_flags,
            }, ensure_ascii=False, default=str))
            return 0

        # --apply
        inserted = _apply_insert(conn, new)
        low_count = sum(1 for c in new if c["confidence"] == "low")
        high_count = sum(1 for c in new if c["confidence"] == "high")
        medium_count = sum(1 for c in new if c["confidence"] == "medium")
        print(f"\n已写入 {inserted} 条（high={high_count}, medium={medium_count} 进表；low={low_count} 仅记录）")
        if inserted > 0 and high_count:
            print(f"  [提示] 新增 high 地址参与净流计算（get_exchange_addresses(chain) 默认读取）")

        print(json.dumps({
            "status": "ok", "dry_run": False, "inserted": inserted,
            "high": high_count, "medium": medium_count, "low": low_count,
            "chains": sorted(chains), "sources": source_flags,
        }, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())