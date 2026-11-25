"""配置与风控闸门.

所有参数来自环境变量，程序不硬编码任何凭证、不输出密钥。
D1~D3 采用方案推荐倾向：A1主+A2对账 / B2比例 / C3降级杠杆。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class RiskGates:
    """L3 风控闸门阈值（方案第七节）."""

    max_single_notional_ratio: float = 0.10   # 单笔 ≤ 分配资金 10%
    max_total_exposure_ratio: float = 0.60    # 总敞口 ≤ 分配资金 60%
    max_symbol_concentration: float = 0.30    # 单品种 ≤ 30%
    max_leverage: float = 3.0                 # 杠杆上限（C3 降级跟随）
    reconcile_tolerance: float = 0.05         # 对账偏差 > 5% 告警+暂停新单
    leader_idle_alert_hours: float = 24.0     # 交易员停摆告警（非故障）

    @classmethod
    def from_env(cls) -> "RiskGates":
        return cls(
            max_single_notional_ratio=_float("CT_MAX_SINGLE_RATIO", 0.10),
            max_total_exposure_ratio=_float("CT_MAX_TOTAL_RATIO", 0.60),
            max_symbol_concentration=_float("CT_MAX_SYMBOL_RATIO", 0.30),
            max_leverage=_float("CT_MAX_LEVERAGE", 3.0),
            reconcile_tolerance=_float("CT_RECON_TOLERANCE", 0.05),
            leader_idle_alert_hours=_float("CT_LEADER_IDLE_H", 24.0),
        )


@dataclass(frozen=True)
class HealthConfig:
    """数据源与交易员健康判据（修硬伤②：两套独立时钟）."""

    poll_interval_sec: int = 15
    backoff_sec: tuple[int, ...] = (2, 4, 6, 8, 16)
    # 数据源故障判据：连续 N 次 fetch 失败 → 数据源故障告警（与交易员无关）
    source_fail_threshold: int = 3
    # 数据陈旧判据：last_successful_fetch 超过此秒数 → 数据源故障（不管有无成交）
    source_stale_sec: int = 180
    # 交易员静默：仅提示，不视为故障（F13 常态）
    leader_idle_hours: float = 24.0

    @classmethod
    def from_env(cls) -> "HealthConfig":
        raw = _env("CT_BACKOFF_SEC", "2,4,6,8,16")
        try:
            backoff = tuple(int(x.strip()) for x in raw.split(",") if x.strip())
        except ValueError:
            backoff = (2, 4, 6, 8, 16)
        return cls(
            poll_interval_sec=_int("CT_POLL_INTERVAL", 15),
            backoff_sec=backoff or (2, 4, 6, 8, 16),
            source_fail_threshold=_int("CT_SOURCE_FAIL_TH", 3),
            source_stale_sec=_int("CT_SOURCE_STALE_SEC", 180),
            leader_idle_hours=_float("CT_LEADER_IDLE_H", 24.0),
        )


@dataclass(frozen=True)
class Config:
    # 目标交易员
    leader_uid: str = "5075281354358777856"

    # 数据源（方案 F5/F6，公开无登录；bapi 是可变私有接口，独立可替换）
    bapi_base: str = "https://bapi.binance.com"
    endpoint_order: str = "/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/order-history"
    endpoint_position: str = "/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/position-history"
    endpoint_detail: str = "/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/detail"
    proxy: str = ""  # 可选；留空走 requests 默认环境

    # 资金映射（D4 由用户定，默认纸面 1000 USDT）
    allocated_capital: float = 1000.0
    # D5 部署位置由用户定，代码只提供入口

    # 模式开关：paper 默认，live 必须显式开启且凭证齐全
    mode: str = "paper"  # paper | live
    api_key: str = ""
    api_secret: str = ""

    # DB：仅用于本地/自建库；不连 prod。留空则用 SQLite 本地文件
    database_url: str = ""

    # 纸面回放：从 fixtures/*.json 读，不发网络请求
    replay_mode: bool = False
    replay_dir: str = "fixtures"

    risk: RiskGates = field(default_factory=RiskGates.from_env)
    health: HealthConfig = field(default_factory=HealthConfig.from_env)

    @classmethod
    def load(cls, base_dir: Path | None = None) -> "Config":
        cfg = cls(
            leader_uid=_env("CT_LEADER_UID", "5075281354358777856"),
            proxy=_env("CT_PROXY", ""),
            allocated_capital=_float("CT_ALLOCATED_CAPITAL", 1000.0),
            mode=_env("CT_MODE", "paper").lower(),
            api_key=_env("CT_API_KEY", ""),
            api_secret=_env("CT_API_SECRET", ""),
            database_url=_env("CT_DATABASE_URL", ""),
            replay_mode=_bool("CT_REPLAY_MODE", False),
            replay_dir=_env("CT_REPLAY_DIR", "fixtures"),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.mode not in {"paper", "live"}:
            raise ValueError(f"CT_MODE 必须是 paper 或 live，收到: {self.mode!r}")
        if self.mode == "live":
            if not self.api_key or not self.api_secret:
                raise ValueError("live 模式必须提供 CT_API_KEY / CT_API_SECRET（用户自填，程序不输出）")
            if self.allocated_capital <= 0:
                raise ValueError("live 模式 CT_ALLOCATED_CAPITAL 必须 > 0")
        if self.risk.max_leverage <= 0:
            raise ValueError("CT_MAX_LEVERAGE 必须 > 0")

    @property
    def is_live(self) -> bool:
        return self.mode == "live"
