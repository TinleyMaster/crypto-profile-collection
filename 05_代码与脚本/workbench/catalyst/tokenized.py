"""
代币化股票 / TradFi 标的的识别与「分离存储」。

背景（2026-10-19，女王大人指示）：
    加密投研链路必须把**代币化美股**（bStocks / xStock / PreStocks / Robinhood Token /
    Backpack Securities / Dinari）与商品期货挡在门外 —— 否则 `HOODBUSDT`（Robinhood
    bStock）这类交易对会被当成加密币，直接污染 `asset_id`。
    但这些数据本身有独立价值（后续要单独做「代币化股票」板块），所以**不丢弃，分离存储**：

        biz.catalyst_tokenized_stock   ←  被剔除的公告 / 被挡下的归属命中

设计原则（重要，改这里之前先读）：

    1. **判定行为零变化**。本模块只是给既有过滤加「分类标签 + 落库落点」，
       不放宽也不收紧任何一条规则。上线的过滤器 `_NON_CRYPTO_NAME_SQL` /
       `_NON_CRYPTO_RE` 的命中集合必须与这里完全一致（有测试盯着）。
    2. **单一真相**。规则表 `TITLE_RULES` / `ASSET_RULES` 是唯一来源，
       布尔判断、分类标签、SQL 正则全部从它派生，避免三处常量各自漂移。
    3. **best-effort**。分离存储失败只 log，绝不影响加密主链路的 ingest。

两种 stage：
    - `source_filter`：源侧闸门（`_exchange_announce.build_item` 返回 None）——
      **公告从未进过 `biz.asset_catalyst`**，这是纯增量捕获。
    - `linker_block`：归属阶段被 `_NON_CRYPTO_NAME_SQL` 挡下的催化剂 ——
      公告已在库里，只是没挂 `asset_id`；由 `collect_tokenized_stock.py` 回扫历史。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# 规则表
# ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Rule:
    """一条识别规则。

    Attributes:
        kind: 机器标签（落 `asset_class` 列）
        label: 中文说明（落 `detect_reason`，给人看）
        pattern: Python 正则源（编译时 re.IGNORECASE）
        pg: PostgreSQL ARE 等价写法（`\\b` 在 PG 里无效，须用 `\\m` / `\\M`）
        strong: 是否「强特征」。强特征 = 明确指向代币化股票/商品衍生，
                可直接作为「分离存储」的入库判据；弱特征（如资产名里带
                `Futures`/`Derivatives` 的裸词）**只用于原过滤**，
                不进分离表 —— 否则 `SynFutures`（真加密项目，F/#6609）会被误收。
    """

    kind: str
    label: str
    pattern: str
    pg: str
    strong: bool = True


# 公告**标题**侧规则（原 `_exchange_announce._NON_CRYPTO_RE` 的逐条拆分）。
# 顺序 = 特异性从高到低，first-match 决定 kind（PG 交替式顺序不影响命中集合）。
TITLE_RULES: tuple[Rule, ...] = (
    Rule("robinhood_token", "Robinhood Token 代币化股票",
         r"robinhood\s+token", r"robinhood[[:space:]]+token"),
    Rule("backpack_securities", "Backpack Securities 代币化股票",
         r"backpack\s+securities", r"backpack[[:space:]]+securities"),
    Rule("prestock", "PreStocks 预上市股票代币",
         r"pre[-\s]?stocks?\b", r"pre[[:space:]-]*stocks?\M"),
    Rule("pre_ipo", "Pre-IPO 一级市场标的",
         r"pre[-\s]?ipo", r"pre[[:space:]-]*ipo"),
    Rule("bstock", "bStocks 代币化股票",
         r"bstocks?", r"bstocks?"),
    Rule("xstock", "xStock 代币化股票",
         r"xstocks?", r"xstocks?"),
    Rule("tokenized_stock", "Tokenized Stocks 代币化股票",
         r"tokeni[sz]ed\s+stocks?", r"tokeni[sz]ed[[:space:]]+stocks?"),
    Rule("tradfi", "TradFi 永续合约",
         r"tradfi", r"tradfi"),
    Rule("equity_perp", "股票永续合约（Stock Perps）",
         r"stock\s+perps?", r"stock[[:space:]]+perps?"),
    Rule("etf_share", "ETF 份额",
         r"etf\s+share", r"etf[[:space:]]+share"),
    Rule("us_equity", "美股指数 / 交易所标的",
         r"nasdaq|nyse", r"nasdaq|nyse"),
    Rule("equity_perp", "股票永续合约（Equity X-Perps）",
         r"equit(?:y|ies)", r"equit(?:y|ies)"),
    Rule("forex", "外汇",
         r"forex", r"forex"),
    Rule("cfd", "差价合约 CFD",
         r"cfds?\b", r"cfds?\M"),
    Rule("metals", "贵金属",
         r"metals?\b", r"metals?\M"),
    Rule("commodity", "大宗商品（原油 / 布伦特 / 天然气 / 商品衍生）",
         r"commodit|crude\s+oil|brent\b|natural\s+gas",
         r"commodit|crude[[:space:]]+oil|brent\M|natural[[:space:]]+gas"),
)

# 资产**名称**侧规则（原 `_NON_CRYPTO_NAME_SQL`，`core.asset.canonical_name`）。
# ⚠️ 与原常量逐字等价 —— 改动会直接改变归属结果，须同步 tests/test_tokenized_split.py。
#
# strong=False 的两条（裸 `futures` / `derivativ`）**只用于原过滤**：
# 实测 `F`(#6609) = `SynFutures`（真加密项目 rank 801），若拿弱特征去筛「分离表」
# 会把它 1000+ 条催化剂误收成「代币化股票」。真正要收的 TradFi 衍生名都带
# 括号后缀（`Crude Oil Futures (Derivatives)` / `Brent Oil (Derivatives)`），
# 已由 `stock_derivative` 这条强规则覆盖。
ASSET_RULES: tuple[Rule, ...] = (
    Rule("prestock", "PreStocks 预上市股票代币",
         r"pre\s*stocks", r"pre[[:space:]]*stocks"),
    Rule("bstock", "bStocks 代币化股票",
         r"b\s*stocks", r"b[[:space:]]*stocks"),
    Rule("robinhood_token", "Robinhood Token 代币化股票",
         r"robinhood\s+token", r"robinhood[[:space:]]+token"),
    Rule("backpack_securities", "Backpack Securities 代币化股票",
         r"backpack\s+securities", r"backpack[[:space:]]+securities"),
    Rule("dinari", "Dinari 代币化股票",
         r"\bdinari\b", r"\mdinari\M"),
    Rule("xstock", "xStock 代币化股票",
         r"\bxstock\b", r"\mxstock\M"),
    Rule("tokenized_stock", "Tokenized Stocks 代币化股票",
         r"tokeniz", r"tokeniz"),
    # 强特征：交易所标注的股票/商品衍生（`… (Derivatives)` / `… (Futures)`）
    Rule("stock_derivative", "交易所标注的股票/商品衍生",
         r"\((?:futures?|derivatives?)\)", r"\((?:futures?|derivatives?)\)"),
    Rule("commodity", "大宗商品（原油 / 布伦特）",
         r"crude\s+oil|brent", r"crude[[:space:]]+oil|brent"),
    # ↓ 弱特征：只参与「是否剔除」判定，不作分离表入库依据
    Rule("futures", "名称含 Futures（弱特征，可能是加密项目名）",
         r"futures", r"futures", strong=False),
    Rule("derivatives", "名称含 Derivatives（弱特征，可能是加密项目名）",
         r"derivativ", r"derivativ", strong=False),
)

# 资产名 SQL 正则（= `_NON_CRYPTO_NAME_SQL`，顺序与原常量保持一致以便逐字对照）
TOKENIZED_ASSET_NAME_SQL: str = (
    r"tokeniz|b[[:space:]]*stocks|pre[[:space:]]*stocks|futures|derivativ"
    r"|crude[[:space:]]+oil|brent"
    r"|robinhood[[:space:]]+token|backpack[[:space:]]+securities"
    r"|\mdinari\M|\mxstock\M"
)

# 「分离存储」专用正则：只含强特征（排除裸 futures/derivativ 弱信号，
# 避免把 SynFutures 这类加密项目当成代币化股票收进分离表）。
STRONG_ASSET_NAME_SQL: str = "|".join(
    r.pg for r in ASSET_RULES if r.strong
)

# 「一包多个 2–6 位代码」= 交易所把代币化股票/ETF 打包上新
# （OKX「to list perpetual futures for H100, ACN, NKE, BWET and SECZ」全为美股；
#   加密币上新是单币句「for DNA crypto」或 `XXX/USDT`，绝不会这样打包）。
# **不带 IGNORECASE**，避免命中普通英文句子（"for the new, and…"）。
_EQUITY_BUNDLE_RE = re.compile(r"\bfor\s+[A-Z0-9]{2,6}(?:\s*,\s*[A-Z0-9]{2,6}){2,}")

_TITLE_COMPILED: tuple[tuple[Rule, re.Pattern[str]], ...] = tuple(
    (r, re.compile(r.pattern, re.IGNORECASE)) for r in TITLE_RULES
)
_ASSET_COMPILED: tuple[tuple[Rule, re.Pattern[str]], ...] = tuple(
    (r, re.compile(r.pattern, re.IGNORECASE)) for r in ASSET_RULES
)


# ──────────────────────────────────────────────────────────────────────
# 判定
# ──────────────────────────────────────────────────────────────────────

def classify_title(title: str) -> tuple[str, str] | None:
    """公告标题 → (kind, 中文标签)；非代币化/TradFi 返回 None。

    ⚠️ 命中集合必须与旧 `_NON_CRYPTO_RE` + `_EQ_BUNDLE_RE` 完全一致。
    """
    if not title:
        return None
    t = title
    for rule, rx in _TITLE_COMPILED:
        if rx.search(t):
            return rule.kind, rule.label
    m = _EQUITY_BUNDLE_RE.search(t)
    if m:
        return "equity_bundle", f"一包多代码（代币化股票/ETF 打包）: {m.group(0)}"
    return None


def is_tokenized_or_tradfi(title: str) -> bool:
    """标题是否属于代币化股票 / TradFi（True = 应剔除出加密链路）。"""
    return classify_title(title) is not None


def classify_asset_name(asset_name: str | None, *, strong_only: bool = False) -> str | None:
    """资产名称 → kind；非代币化/衍生资产返回 None。

    Args:
        strong_only: True 时只用强特征规则（分离存储入库判据）；
            False 时等价旧 `_NON_CRYPTO_NAME_SQL`（归属过滤口径）。
    """
    if not asset_name:
        return None
    for rule, rx in _ASSET_COMPILED:
        if strong_only and not rule.strong:
            continue
        if rx.search(asset_name):
            return rule.kind
    return None


def is_tokenized_asset(asset_name: str | None, *, strong_only: bool = False) -> bool:
    """资产名称是否属代币化股票 / 商品衍生。

    strong_only=False（默认）等价旧 `_NON_CRYPTO_NAME_SQL`；
    strong_only=True 为「分离存储」判据。
    """
    return classify_asset_name(asset_name, strong_only=strong_only) is not None


# ──────────────────────────────────────────────────────────────────────
# 分离存储
# ──────────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS biz.catalyst_tokenized_stock (
    id              bigserial PRIMARY KEY,
    stage           varchar(24)  NOT NULL,
    source_code     varchar(64)  NOT NULL,
    source_item_id  varchar(200) NOT NULL,
    title           text         NOT NULL,
    published_at    timestamptz,
    source_url      text,
    symbols         text[],
    asset_class     varchar(32),
    detect_reason   text,
    catalyst_id     bigint,
    raw_json        jsonb,
    created_at      timestamptz  NOT NULL DEFAULT now(),
    UNIQUE (stage, source_code, source_item_id)
);
CREATE INDEX IF NOT EXISTS idx_cts_published
    ON biz.catalyst_tokenized_stock (published_at DESC);
CREATE INDEX IF NOT EXISTS idx_cts_class
    ON biz.catalyst_tokenized_stock (asset_class);
CREATE INDEX IF NOT EXISTS idx_cts_catalyst
    ON biz.catalyst_tokenized_stock (catalyst_id);
COMMENT ON TABLE biz.catalyst_tokenized_stock IS
    '代币化股票/TradFi 标的的分离存储（不进加密投研链路，供后续独立板块使用）';
"""

_TABLE_READY = False


def ensure_table(conn) -> None:
    """幂等建表（进程内只跑一次）。"""
    global _TABLE_READY
    if _TABLE_READY:
        return
    with conn.cursor() as cur:
        cur.execute(_DDL)
    conn.commit()
    _TABLE_READY = True
    logger.info("biz.catalyst_tokenized_stock 就绪")


def build_row(
    *,
    stage: str,
    source_code: str,
    source_item_id: str,
    title: str,
    published_at=None,
    source_url: str = "",
    symbols: list[str] | None = None,
    asset_class: str | None = None,
    detect_reason: str = "",
    catalyst_id: int | None = None,
    raw: dict | None = None,
) -> dict:
    """构造待落库行（字段与 DDL 对齐）。"""
    return {
        "stage": stage,
        "source_code": (source_code or "")[:64],
        "source_item_id": (str(source_item_id or "")[:200] or "-"),
        "title": title or "",
        "published_at": published_at,
        "source_url": source_url or "",
        "symbols": list(symbols or []),
        "asset_class": (asset_class or "")[:32] or None,
        "detect_reason": detect_reason or "",
        "catalyst_id": catalyst_id,
        "raw_json": json.dumps(raw, ensure_ascii=False, default=str) if raw else None,
    }


_UPSERT = """
INSERT INTO biz.catalyst_tokenized_stock
    (stage, source_code, source_item_id, title, published_at, source_url,
     symbols, asset_class, detect_reason, catalyst_id, raw_json)
VALUES
    (%(stage)s, %(source_code)s, %(source_item_id)s, %(title)s, %(published_at)s,
     %(source_url)s, %(symbols)s, %(asset_class)s, %(detect_reason)s,
     %(catalyst_id)s, %(raw_json)s)
ON CONFLICT (stage, source_code, source_item_id) DO UPDATE SET
    title         = EXCLUDED.title,
    published_at  = COALESCE(EXCLUDED.published_at, biz.catalyst_tokenized_stock.published_at),
    source_url    = COALESCE(NULLIF(EXCLUDED.source_url, ''), biz.catalyst_tokenized_stock.source_url),
    symbols       = EXCLUDED.symbols,
    asset_class   = COALESCE(EXCLUDED.asset_class, biz.catalyst_tokenized_stock.asset_class),
    detect_reason = EXCLUDED.detect_reason,
    catalyst_id   = COALESCE(EXCLUDED.catalyst_id, biz.catalyst_tokenized_stock.catalyst_id),
    raw_json      = COALESCE(EXCLUDED.raw_json, biz.catalyst_tokenized_stock.raw_json)
"""


def persist(conn, rows: list[dict], *, commit: bool = True) -> int:
    """批量写入分离表，返回提交行数（幂等：同 (stage, source_code, source_item_id) 覆盖）。"""
    if not rows:
        return 0
    ensure_table(conn)
    with conn.cursor() as cur:
        cur.executemany(_UPSERT, rows)
    if commit:
        conn.commit()
    return len(rows)


def persist_safely(conn, rows: list[dict]) -> int:
    """落库封装：任何异常只 log 不抛（分离存储不得影响主链路）。"""
    try:
        return persist(conn, rows)
    except Exception as e:                                  # noqa: BLE001
        logger.warning("代币化股票分离存储失败（不影响主链路）：%s", e)
        try:
            conn.rollback()
        except Exception:                                   # noqa: BLE001
            pass
        return 0
