#!/usr/bin/env python3
"""GDELT 2.0 DOC API 客户端（全免费、无 key、全历史新闻，按关键词+时间查询）。

背景（2026-10-03）：BTC/ETH 爆仓极值日的**历史**新闻归因需要免费资讯源。
项目内 catalyst 管线只覆盖近期（近几日），2021/2022 等历史事件查不到 ⇒ 接入 GDELT。

接口：
    GET https://api.gdeltproject.org/api/v2/doc/doc
        ?query=<关键词语法>&mode=tone|artlist|timelinevol|timelinetone&format=json
        &startdatetime=YYYYMMDDHHMMSS&enddatetime=YYYYMMDDHHMMSS&maxrecords=250

⚠️ 实测边界（2026-10-03 probe，勿重复探测）：
  - **限频 1 req / 5s（严格，超则 HTTP 429）**；共享 IP 下更严，必须指数退避重试。
  - `mode=tone`：返回 {TotalArticles, MatchedArticles, AvgTone, ...}，1 请求即可得
    新闻量 + 情感，适合全量回填。
  - `mode=artlist`：返回文章列表（每篇 url/title/domain/...），单日单关键词实测 25 篇，
    `tone` 字段为 None（tone 只在 mode=tone 提供）。
  - 查询语法：`bitcoin`（单词）、`"bitcoin"`（短语）、`bitcoin OR ethereum`、
    `sourcelang:eng`（限定英文）、`domain:coindesk.com`（限定域名）。
  - 历史覆盖：DOC 2.0 自 2017 年起（15 分钟粒度）。

用法：
    from crypto_research.clients.gdelt_client import GDELTClient
    c = GDELTClient()
    stats = c.tone_stats("bitcoin", datetime(2026,8,19), datetime(2026,8,19))
    arts  = c.articles("bitcoin", d, d, max_records=25)
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from crypto_research.config import get_settings  # noqa: E402

BASE_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
TIMEOUT = 30
# ⚠️ 严格限频：官方要求 1 req / 5s。共享 IP 实测 5s 内仍可能 429 ⇒ 保守 5.5s。
MIN_GAP_S = 5.5
# 429 退避：5→10→20→40s，上限 4 次重试后抛错（保回填可 --resume 续跑）
RETRY_BACKOFFS = (5, 10, 20, 40)
MAX_RETRIES = 4


class GDELTRateLimit(RuntimeError):
    """连续 429 重试耗尽——调用方记录后跳过该事件（回填不中断）。"""


class GDELTClient:
    def __init__(self, base_url: str = BASE_URL, min_gap_s: float = MIN_GAP_S,
                 proxy: str | None = None):
        self.base_url = base_url
        self.min_gap_s = min_gap_s
        self.max_retries = MAX_RETRIES
        self.retry_backoffs = RETRY_BACKOFFS
        self._last_ts = 0.0
        self._session = requests.Session()
        # ⚠️ GDELT 按 IP 限频（1 req/5s，共享/云 IP 会持久 429）。走代理换出口 IP 可绕开。
        # 未显式传 proxy 时回退项目配置（.env 的 HTTPS_PROXY / http_proxy）。
        if proxy is None:
            try:
                proxy = get_settings().https_proxy
            except Exception:  # noqa: BLE001 配置缺失时静默直连
                proxy = None
        if proxy:
            self._session.proxies.update({"http": proxy, "https": proxy})

    # ── 内部：限频 + 退避 ──────────────────────────────────────
    def _throttle(self) -> None:
        wait = self.min_gap_s - (time.monotonic() - self._last_ts)
        if wait > 0:
            time.sleep(wait)

    def _get(self, params: dict[str, Any]) -> dict:
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                r = self._session.get(self.base_url, params=params, timeout=TIMEOUT)
                self._last_ts = time.monotonic()
                if r.status_code == 429:
                    wait = self.retry_backoffs[min(attempt, len(self.retry_backoffs) - 1)]
                    last_err = GDELTRateLimit(f"429，退避 {wait}s: {r.text[:120]}")
                    time.sleep(wait)
                    continue
                if r.status_code != 200:
                    last_err = RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
                    time.sleep(2 * (attempt + 1))
                    continue
                try:
                    return r.json()
                except Exception as e:  # noqa: BLE001
                    # mode=timeline 等返回 text/csv 时不走 JSON；本客户端只用 json 模式
                    last_err = RuntimeError(f"非 JSON 响应: {r.text[:200]} ({e})")
                    time.sleep(2)
                    continue
            except GDELTRateLimit:
                raise
            except Exception as e:  # noqa: BLE001 网络抖动
                last_err = e
                time.sleep(2 * (attempt + 1))
        raise last_err or GDELTRateLimit("未知错误")

    # ── 查询构造 ───────────────────────────────────────────────
    @staticmethod
    def _fmt(dt: datetime) -> str:
        return dt.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")

    def _query(self, query: str, mode: str,
               start: datetime, end: datetime,
               max_records: int = 250) -> dict:
        return self._get({
            "query": query, "mode": mode, "format": "json",
            "startdatetime": self._fmt(start), "enddatetime": self._fmt(end),
            "maxrecords": max_records,
        })

    # ── 高层接口 ───────────────────────────────────────────────
    def tone_stats(self, query: str, start: datetime, end: datetime) -> dict:
        """mode=tone：{TotalArticles, MatchedArticles, AvgTone, ...}。单请求。

        返回空 dict 表示无匹配（GDELT 对 0 匹配返回空 json 或字段缺失）。
        """
        data = self._query(query, "tone", start, end)
        return data if isinstance(data, dict) else {}

    def articles(self, query: str, start: datetime, end: datetime,
                 max_records: int = 25) -> list[dict]:
        """mode=artlist：文章列表 [{url, title, domain, language, ...}]。"""
        data = self._query(query, "artlist", start, end, max_records=max_records)
        return data.get("articles", []) if isinstance(data, dict) else []

    def day_tone_stats(self, query: str, day: datetime) -> dict:
        """单日（UTC 0 点 ~ 次日 0 点）tone 统计，供事件归因。"""
        day0 = day.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
        return self.tone_stats(query, day0, day0 + timedelta(days=1) - timedelta(seconds=1))

    def day_articles(self, query: str, day: datetime, max_records: int = 25) -> list[dict]:
        """单日（UTC）文章列表，供代表性事件深挖。"""
        day0 = day.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
        return self.articles(query, day0, day0 + timedelta(days=1) - timedelta(seconds=1),
                             max_records=max_records)

    # ── timeline 序列（低请求量方案，2026-10-03 新增） ──────────
    # 背景：GDELT 按 IP 限频（1 req/5s，共享 IP 常 429）。逐事件查询（~576 次）不可行。
    # 官方文档：timeline 模式跨度 >1 周 ⇒ 按**日**分桶；每序列上限 ~250 点。
    # ⇒ 按 ≤180 天窗口切段，每段 1 次 timelinevolraw（文章数）+ 1 次 timelinetone（情感），
    #   全量 ~50 次请求即得整段日频序列，事件归因本地查表。
    TIMELINE_WINDOW_DAYS = 180

    def _timeline_one(self, query: str, mode: str, start: datetime, end: datetime,
                      retries: int | None = None) -> list[dict]:
        """单段 timeline 请求。返回**数据点数组**（把 series 包装解开）。

        GDELT 实测返回 `[{"series": "Article Count", "data": [{date,value,...}, ...]}]`。
        429 时按实例退避重试。
        """
        old_max, old_bw = self.max_retries, self.retry_backoffs
        if retries is not None:
            self.max_retries = retries
        try:
            data = self._query(query, mode, start, end)
        finally:
            self.max_retries, self.retry_backoffs = old_max, old_bw
        if not isinstance(data, dict):
            return []
        out: list[dict] = []
        for series in data.get("timeline", []) or []:
            out.extend(series.get("data", []) or [])
        return out

    @staticmethod
    def _parse_tl_date(raw: str) -> str | None:
        """'20260814T000000Z' → '2026-08-14'（GDELT timeline 专用格式）。"""
        if not raw or len(raw) < 8:
            return None
        y, m, d = raw[:4], raw[4:6], raw[6:8]
        if not (y.isdigit() and m.isdigit() and d.isdigit()):
            return None
        return f"{y}-{m}-{d}"

    def timeline_daily_series(self, query: str, start: datetime, end: datetime,
                              retries: int = 3, cache_path: str | None = None,
                              initial: dict | None = None) -> dict[str, dict]:
        """按日新闻序列（timelinevolraw 文章数 + timelinetone 情感）。

        返回 {YYYY-MM-DD: {"count": int|None, "tone": float|None}}，缺段日不补 0。
        窗口内单段失败 ⇒ 该段整段缺失（不中断，调用方标记）。

        ⚠️ 粒度兜底（2026-10-03 实测）：GDELT 按时间跨度自动定桶——
        跨度 < 1 周 ⇒ **小时桶**（24 点/天），≥ 1 周 ⇒ 日桶。本方法对小时桶按日
        **求和/平均**聚合，保证输出恒为日频（段长 ≥ 180 天时恒为日桶，此分支仅兜底）。
        `cache_path`：每段完成后写盘（断点续跑）；`initial`：已有缓存，段已覆盖则跳过。
        """
        out: dict[str, dict] = {**initial} if initial else {}
        cursor = start.astimezone(timezone.utc)
        end = end.astimezone(timezone.utc)
        while cursor < end:
            seg_end = min(cursor + timedelta(days=self.TIMELINE_WINDOW_DAYS), end)
            # 段长 < 7 天时 GDELT 会回小时桶 ⇒ 段长至少 14 天（也兜底聚合逻辑）
            if (seg_end - cursor).days < 14:
                seg_end = cursor + timedelta(days=14)
                if seg_end > end:
                    seg_end = end
            # 断点续跑：段内全部日期已有任意字段 ⇒ 跳过
            if initial:
                seg_dates = [cursor + timedelta(days=i)
                             for i in range((seg_end - cursor).days)]
                if seg_dates and all(any(k in out.get(d.isoformat(), {})
                                         for k in ("count", "tone")) for d in seg_dates):
                    print(f"[gdelt] {query} 段 {cursor.date()}~{seg_end.date()}: 已覆盖，跳过")
                    cursor = seg_end
                    continue
            vol_ok = tone_ok = False
            try:
                for pt in self._timeline_one(query, "timelinevolraw", cursor, seg_end, retries=retries):
                    d = self._parse_tl_date(pt.get("date", ""))
                    if not d:
                        continue
                    rec = out.setdefault(d, {})
                    rec["count"] = (rec.get("count") or 0) + (pt.get("value") or 0)
                vol_ok = True
            except Exception:  # noqa: BLE001 段失败 → 缺失，不中断
                pass
            self._throttle()  # 模式切换间同样限频
            try:
                tones: dict[str, list[float]] = {}
                for pt in self._timeline_one(query, "timelinetone", cursor, seg_end, retries=retries):
                    d = self._parse_tl_date(pt.get("date", ""))
                    t = pt.get("tone")
                    if d and t is not None:
                        tones.setdefault(d, []).append(float(t))
                for d, vals in tones.items():
                    rec = out.setdefault(d, {})
                    rec["tone"] = sum(vals) / len(vals)  # 小时桶→日均
                tone_ok = True
            except Exception:  # noqa: BLE001
                pass
            if cache_path:
                try:
                    Path(cache_path).write_text(
                        __import__("json").dumps(out, ensure_ascii=False), encoding="utf-8")
                except Exception:  # noqa: BLE001
                    pass
            print(f"[gdelt] {query} 段 {cursor.date()}~{seg_end.date()}: "
                  f"vol={vol_ok} tone={tone_ok} 点={len(out)}")
            cursor = seg_end
        return out
