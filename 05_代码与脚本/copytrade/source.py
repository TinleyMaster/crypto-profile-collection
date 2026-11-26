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
    """实时数据源。修三个真实网络才暴露的坑：
      - 代理单点 502 → 候选池轮换 + 成功后黏性复用
      - indexValue 方向：实测往【历史】翻页（page2 拿到更早订单），
        故热运行每轮只拉【最新一页】+ raw_order dedup，绝不拿游标续传（会漏新单）；
        游标仅用于冷启动全量回填（bootstrap）。
      - 请求头必须带完整浏览器指纹，否则 202/403 边缘风控。
    """

    def __init__(self, cfg: Config, store: Any, logger: Callable[[str], None] | None = None):
        self.cfg = cfg
        self.store = store
        self.log = logger or (lambda m: None)
        self.session = requests.Session()
        # 关键：环境里 HTTPS_PROXY=60459 是死代理，会覆盖 session.proxies 导致 502。
        # 禁用 env 继承，完全由候选池控制代理。
        self.session.trust_env = False
        self.session.headers.update({
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.binance.com/",
            "clienttype": "web",
        })
        # 代理候选池：首选 CT_PROXY，再 CT_PROXY_CANDIDATES，最后直连兜底
        cands: list[str | None] = []
        if cfg.proxy:
            cands.append(cfg.proxy)
        for raw in cfg.proxy_candidates.split(","):
            p = raw.strip()
            if p and p not in cands:
                cands.append(p)
        # 直连仅在显式配置 CT_PROXY_CANDIDATES=direct 时加入（本机实测直连不通，默认不放进池）
        if "direct" in [c.strip().lower() for c in cfg.proxy_candidates.split(",")]:
            cands.append(None)
        self._proxies: list[str | None] = cands or [None]
        self._proxy_idx = 0
        self._apply_proxy()

    def _apply_proxy(self) -> None:
        p = self._proxies[self._proxy_idx]
        self.session.proxies = {"http": p, "https": p} if p else {}

    def _rotate_proxy(self) -> None:
        self._proxy_idx = (self._proxy_idx + 1) % len(self._proxies)
        self._apply_proxy()
        self.log(f"[proxy] 切换到候选 #{self._proxy_idx}: {self._proxies[self._proxy_idx] or '直连'}")

    # ---------- 公开 API ----------
    def fetch_all(self, bootstrap: bool = False) -> dict[str, FetchResult]:
        """热运行：只拉最新一页。bootstrap：用 indexValue 往历史全量回填。"""
        out = {
            "order": self._fetch_with_retry(
                "order", self.cfg.endpoint_order, self._parse_order, bootstrap=bootstrap),
            "position": self._fetch_with_retry(
                "position", self.cfg.endpoint_position, self._parse_position, bootstrap=bootstrap),
            "detail": self._fetch_with_retry(
                "detail", self.cfg.endpoint_detail, self._parse_detail, bootstrap=False),
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
    def _request_once(self, name: str, endpoint: str, body: dict | None,
                      params: dict | None) -> tuple[FetchResult, dict | None]:
        """单次 HTTP 请求（当前代理）。返回 (结果, payload)。网络层失败时 ok=False。"""
        url = self.cfg.bapi_base + endpoint
        t0 = time.monotonic()
        try:
            if params is not None:
                r = self.session.get(url, params=params, timeout=20, allow_redirects=False)
            else:
                r = self.session.post(url, json=body, timeout=20, allow_redirects=False)
            elapsed = int((time.monotonic() - t0) * 1000)
            redirected = r.status_code in (301, 302, 303, 307, 308, 202)
            if r.status_code != 200 or redirected:
                return FetchResult(source=name, ok=False, http_status=r.status_code,
                                   redirected=redirected, elapsed_ms=elapsed,
                                   error=f"HTTP {r.status_code}" + (" 边缘风控/重定向" if redirected else "")), None
            payload = r.json()
            code = str(payload.get("code", ""))
            if code != "000000":
                return FetchResult(source=name, ok=False, raw=payload, elapsed_ms=elapsed,
                                   error=f"业务码 {code}: {str(payload.get('message',''))[:80]}"), payload
            return FetchResult(source=name, ok=True, raw=payload, elapsed_ms=elapsed,
                               http_status=200), payload
        except requests.RequestException as e:
            return FetchResult(source=name, ok=False,
                               error=f"{type(e).__name__}: {str(e)[:140]}"), None
        except (ValueError, json.JSONDecodeError) as e:
            return FetchResult(source=name, ok=False, error=f"JSON 解析失败: {str(e)[:120]}"), None

    def _fetch_with_retry(self, name: str, endpoint: str, parser: Callable[[dict], list[dict]],
                          bootstrap: bool = False) -> FetchResult:
        """代理轮换重试 + 分页。热运行只拉最新页；bootstrap 往历史翻页全量回填。

        2026-11-26 实测：indexValue 是【历史方向】游标（带游标拿到更早订单），
        因此绝不能用于常规增量续传——否则每轮拿同一批最新单、超过 pageSize 后漏新单。
        """
        if name == "detail":
            params: dict | None = {"portfolioId": self.cfg.leader_uid}
            body: dict | None = None
        else:
            params = None
            body = {"portfolioId": self.cfg.leader_uid, "pageSize": self.cfg.page_size}

        # 重试策略：同一代理先退避重试 2 次（代理 7890 是间歇 502，快速轮换只会加剧），
        # 连续失败才轮换代理；每个代理至少给 3 次机会。
        per_proxy = 3
        max_rounds = max(3, len(self._proxies) * per_proxy)
        all_rows: list[dict] = []
        seen_keys: set[str] = set()
        cursor: int | None = None
        pages = 0
        last_err = ""
        total = 0

        while pages < (self.cfg.bootstrap_max_pages if bootstrap else 1):
            if body is not None and cursor is not None:
                body = {**body, "indexValue": cursor}
            got = False
            same_proxy_fails = 0
            for attempt in range(max_rounds):
                res, payload = self._request_once(name, endpoint, body, params)
                if res.ok and payload is not None:
                    got = True
                    break
                last_err = res.error
                same_proxy_fails += 1
                rate_limited = any(c in res.error for c in
                                   ("11012005", "90801003", "Too many", "系统忙", "429"))
                if rate_limited:
                    # 币安限流：退避 5s 起递增，不换代理（换代理没用，是业务限流）
                    time.sleep(5 + attempt * 3)
                elif same_proxy_fails >= per_proxy:
                    self._rotate_proxy()
                    same_proxy_fails = 0
                    time.sleep(1)
                else:
                    # 网络层失败：短退避给代理恢复窗口
                    time.sleep(2 + min(attempt, 3))
            if not got:
                return FetchResult(source=name, ok=False, error=f"{last_err}（{max_rounds}次尝试均失败）")

            rows = parser(payload)
            for row in rows:
                k = _dedup_key(_normalize_row(row))
                if k not in seen_keys:
                    seen_keys.add(k)
                    all_rows.append(_normalize_row(row))
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            total = int(data.get("total") or 0)
            pages += 1
            self.log(f"[{name}] page{pages} +{len(rows)} 累计唯一={len(all_rows)}/{total}")

            if not bootstrap or not rows or len(all_rows) >= total:
                break
            nxt = data.get("indexValue")
            if not nxt or int(nxt) == cursor:
                break
            cursor = int(nxt)
            time.sleep(3)  # 冷启动翻页节流，避免触发 11012005 限流

        result = FetchResult(source=name, ok=True, rows=all_rows, raw=payload or {},
                             http_status=200, elapsed_ms=0)
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            iv = payload["data"].get("indexValue")
            if iv:
                result.raw["_next_cursor"] = str(iv)
        return result

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

    def fetch_all(self, bootstrap: bool = False) -> dict[str, FetchResult]:
        # bootstrap 参数仅为与 BinanceCopySource 签名兼容；回放按批次推进
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
