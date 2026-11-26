"""L1 数据源层 — 币安跟单公开接口（order-history 主 + position-history 对账 + detail）。

修硬伤①（数据源单点）：三源互备，任一源失败不影响其余源；bapi 是可变私有接口，
        统一封装在 fetch() 一处，将来换源只改这里。
修硬伤②（watchdog 失效）：双时钟判据——
        - 数据源健康看 sync_state.last_success_fetch_at（成功取到响应即刷新，与有无新单无关）
        - 交易员活跃看 sync_state.last_trade_at（来自数据内容，仅提示不告警为故障）
        两者严格分开，避免「他 20h 没交易」被误判成「数据源挂了」。
先存后算：原始 JSON 先落 raw_order/raw_position，可离线重放。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

from config import Config

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


@dataclass
class FetchResult:
    source: str
    ok: bool
    rows: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    http_status: int = 0
    redirected: bool = False
    elapsed_ms: int = 0

    @property
    def is_antifraud(self) -> bool:
        """302→首页 / 202 / 418 都视为边缘风控，不是业务错误。"""
        return self.redirected or self.http_status in (202, 418, 403)


class BinanceCopySource:
    def __init__(self, cfg: Config, store: Any, logger: Callable[[str], None] | None = None):
        self.cfg = cfg
        self.store = store
        self.log = logger or (lambda m: None)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.binance.com/",
            "clienttype": "web",
        })
        if cfg.proxy:
            self.session.proxies.update({"http": cfg.proxy, "https": cfg.proxy})

    # ---------- 公开 API ----------
    def fetch_all(self) -> dict[str, FetchResult]:
        out = {
            "order": self._fetch_with_retry("order", self.cfg.endpoint_order, self._parse_order),
            "position": self._fetch_with_retry("position", self.cfg.endpoint_position, self._parse_position),
            "detail": self._fetch_with_retry("detail", self.cfg.endpoint_detail, self._parse_detail),
        }
        self._update_health(out)
        return out

    def check_health(self) -> dict[str, Any]:
        """双时钟判据返回结构化健康状态（watchdog 用）。"""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        sync = self.store.get_sync("order")  # 主源为准
        out: dict[str, Any] = {
            "source_healthy": False,
            "source_reason": "",
            "leader_idle": False,
            "leader_idle_hours": 0.0,
            "last_success_fetch_at": sync.get("last_success_fetch_at"),
            "last_trade_at": sync.get("last_trade_at"),
            "consecutive_fail": sync.get("consecutive_fail", 0),
        }
        # 时钟 A：数据源故障（连续失败 或 成功时钟陈旧）
        if int(sync.get("consecutive_fail", 0)) >= self.cfg.health.source_fail_threshold:
            out["source_reason"] = f"连续 {sync['consecutive_fail']} 次取数失败: {sync.get('last_error', '')[:120]}"
            return out
        last_ok = _parse_iso(sync.get("last_success_fetch_at"))
        if last_ok is None:
            out["source_reason"] = "从未成功取数（首次启动或全失败）"
            return out
        age = (now - last_ok).total_seconds()
        if age > self.cfg.health.source_stale_sec:
            out["source_reason"] = f"数据源成功时钟陈旧 {age/60:.1f} 分钟 > 阈值 {self.cfg.health.source_stale_sec}s"
            return out
        out["source_healthy"] = True
        # 时钟 B：交易员静默（仅提示，绝不判为数据源故障）
        last_trade = _parse_iso(sync.get("last_trade_at"))
        if last_trade:
            idle_h = (now - last_trade).total_seconds() / 3600
            out["leader_idle_hours"] = round(idle_h, 2)
            out["leader_idle"] = idle_h >= self.cfg.health.leader_idle_hours
        return out

    # ---------- 内部 ----------
    def _fetch_with_retry(self, name: str, endpoint: str, parser: Callable[[dict], list[dict]]) -> FetchResult:
        """按 2026-11-26 真实抓包核验的正确协议请求。

        detail  : GET  ?portfolioId=<id>
        history : POST JSON {"portfolioId":<id>,"pageSize":100[,"indexValue":<cursor>]}
        错误码：000000 成功 / 000002 参数非法 / 11012005 系统忙(限流) / 90801003 请求过多
        """
        url = self.cfg.bapi_base + endpoint
        # 真实核验：参数名是 portfolioId，不是 uid
        if name == "detail":
            request_kw = {"params": {"portfolioId": self.cfg.leader_uid}, "method": "get"}
        else:
            body: dict[str, Any] = {"portfolioId": self.cfg.leader_uid, "pageSize": 100}
            # indexValue 是分页游标（不是时间），从上次成功游标续传
            sync = self.store.get_sync(name)
            cursor = sync.get("last_cursor")
            if cursor:
                body["indexValue"] = int(cursor)
            request_kw = {"json": body, "method": "post"}

        last = FetchResult(source=name, ok=False)
        for attempt, backoff in enumerate([0, *self.cfg.health.backoff_sec, 6, 6]):
            if backoff:
                time.sleep(backoff)
            t0 = time.monotonic()
            try:
                if request_kw["method"] == "get":
                    r = self.session.get(url, params=request_kw["params"], timeout=20,
                                         allow_redirects=False)
                else:
                    r = self.session.post(url, json=request_kw["json"], timeout=20,
                                          allow_redirects=False)
                elapsed = int((time.monotonic() - t0) * 1000)
                redirected = r.status_code in (301, 302, 303, 307, 308)
                if r.status_code != 200 or redirected:
                    last = FetchResult(
                        source=name, ok=False, http_status=r.status_code,
                        redirected=redirected, elapsed_ms=elapsed,
                        error=f"HTTP {r.status_code}" + (" 302->首页(域名/风控)" if redirected else ""),
                    )
                    self.log(f"[{name}] attempt{attempt} {last.error}")
                    continue
                payload = r.json()
                code = str(payload.get("code", ""))
                if code != "000000":
                    last = FetchResult(source=name, ok=False, raw=payload, elapsed_ms=elapsed,
                                       error=f"业务码 {code}: {str(payload.get('message',''))[:80]}")
                    self.log(f"[{name}] attempt{attempt} {last.error}")
                    continue
                rows = parser(payload)
                # 字段名归一化：真实接口给 avgPrice，状态机统一读 price
                rows = [_normalize_row(x) for x in rows]
                last = FetchResult(source=name, ok=True, rows=rows, raw=payload,
                                   http_status=200, elapsed_ms=elapsed)
                # 把新游标（indexValue）带给健康更新层落库
                if isinstance(payload.get("data"), dict) and payload["data"].get("indexValue"):
                    last.raw["_next_cursor"] = str(payload["data"]["indexValue"])
                return last
            except requests.RequestException as e:
                last = FetchResult(source=name, ok=False, error=f"{type(e).__name__}: {str(e)[:160]}")
                self.log(f"[{name}] attempt{attempt} 网络异常 {last.error}")
            except (ValueError, json.JSONDecodeError) as e:
                last = FetchResult(source=name, ok=False, error=f"JSON 解析失败: {str(e)[:120]}")
        return last

    def _update_health(self, results: dict[str, FetchResult]) -> None:
        """每个源独立记成功/失败时钟，互不影响。"""
        for name, res in results.items():
            if not res.ok:
                self.store.mark_fetch_fail(name, res.error)
                continue
            # 从数据内容提取最后成交时间（只在 order 源上有意义）
            last_trade_iso = None
            if name == "order" and res.rows:
                ts = max((r.get("orderTime") for r in res.rows if r.get("orderTime")), default=None)
                if ts:
                    last_trade_iso = datetime.fromtimestamp(int(ts) / 1000, timezone.utc).isoformat()
            self.store.mark_fetch_success(
                source=name,
                has_data=bool(res.rows),
                last_trade_at=last_trade_iso,
                cursor=res.raw.get("_next_cursor"),
            )
            # 原始 JSON 落库统一由 daemon 处理（避免双重入库导致信号被判重跳过）
            # source 层只负责取数 + 更新健康时钟。

    @staticmethod
    def _parse_order(payload: dict) -> list[dict]:
        data = payload.get("data")
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            for k in ("orderList", "list", "rows", "orders"):
                if isinstance(data.get(k), list):
                    return [x for x in data[k] if isinstance(x, dict)]
        return []

    @staticmethod
    def _parse_position(payload: dict) -> list[dict]:
        data = payload.get("data")
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            for k in ("positionList", "list", "rows", "positions"):
                if isinstance(data.get(k), list):
                    return [x for x in data[k] if isinstance(x, dict)]
        return []

    @staticmethod
    def _parse_detail(payload: dict) -> list[dict]:
        data = payload.get("data")
        return [data] if isinstance(data, dict) else []


class ReplaySource:
    """离线回放源：从 fixtures/*.json 读取，不发任何网络请求。

    用于 H1/H2 验证（本地浏览器抓包样本）、状态机回放、无网络自测。
    """

    def __init__(self, cfg: Config, store: Any, logger: Callable[[str], None] | None = None):
        self.cfg = cfg
        self.store = store
        self.log = logger or (lambda m: None)
        self.dir = Path(__file__).parent / cfg.replay_dir
        self._batches: list[dict[str, list[dict]]] = []
        self._idx = 0
        self._load()

    def _load(self) -> None:
        if not self.dir.exists():
            return
        files = sorted(self.dir.glob("*.json"))
        for f in files:
            try:
                payload = json.loads(f.read_text(encoding="utf-8"))
            except Exception as e:
                self.log(f"[replay] 跳过坏文件 {f.name}: {e}")
                continue
            if not isinstance(payload, dict):
                continue
            batch = {
                "order": payload.get("order", []),
                "position": payload.get("position", []),
                "detail": payload.get("detail", []),
            }
            for k in batch:
                if isinstance(batch[k], dict):
                    batch[k] = [batch[k]]
                if not isinstance(batch[k], list):
                    batch[k] = []
            self._batches.append(batch)
        self._batches.sort(key=lambda b: max(
            (r.get("orderTime", 0) for r in b["order"] if isinstance(r, dict)), default=0
        ))
        self.log(f"[replay] 加载 {len(self._batches)} 批样本")

    def fetch_all(self) -> dict[str, FetchResult]:
        if self._idx >= len(self._batches):
            batch = {"order": [], "position": [], "detail": []}
        else:
            batch = self._batches[self._idx]
            self._idx += 1
        out: dict[str, FetchResult] = {}
        for name, rows in batch.items():
            rows = [_normalize_row(r) for r in rows]
            out[name] = FetchResult(source=name, ok=True, rows=rows, http_status=200)
            # 原始落库统一由 daemon 处理（与 BinanceCopySource 口径一致）
            # 回放模式把成功时钟和最后成交时间也推进，健康判据同样可测
            ts = max((r.get("orderTime") for r in rows if r.get("orderTime")), default=None)
            last_trade = datetime.fromtimestamp(int(ts) / 1000, timezone.utc).isoformat() if ts else None
            self.store.mark_fetch_success(name, has_data=bool(rows), last_trade_at=last_trade)
        return out

    def check_health(self) -> dict[str, Any]:
        # 复用同一判据，保证回放和实盘口径一致
        return BinanceCopySource.check_health(self)

    @property
    def exhausted(self) -> bool:
        return self._idx >= len(self._batches)


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    """真实接口字段名 → 状态机内部统一字段名（2026-11-26 核验）。

    真实 order-history 字段：symbol/side/positionSide/executedQty/avgPrice/totalPnl/
    orderTime/orderUpdateTime —— 价格键是 avgPrice，不是 price！
    真实 position-history 字段：positionId/symbol/side(Long/Short)/avgCost/avgClosePrice/
    closedVolume/leverage/status —— side 是 Long/Short 首字母大写，需映射为 LONG/SHORT。
    """
    out = dict(row)
    if "price" not in out and "avgPrice" in out:
        out["price"] = out["avgPrice"]
    if "price" not in out and "avgCost" in out:
        out["price"] = out["avgCost"]
    side = str(out.get("positionSide") or "")
    if not side and out.get("side") in ("Long", "Short", "LONG", "SHORT"):
        out["positionSide"] = str(out["side"]).upper()
    return out


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def _dedup_key(row: dict[str, Any]) -> str:
    """唯一键：orderTime+symbol+side+positionSide+executedQty 稳定序列化。

    不使用 indexValue（实测它是分页游标，非时间）。
    """
    parts = [
        str(row.get("orderTime", "")),
        str(row.get("symbol", "")),
        str(row.get("side", "")),
        str(row.get("positionSide", "")),
        str(row.get("executedQty", "")),
    ]
    return "|".join(parts)
