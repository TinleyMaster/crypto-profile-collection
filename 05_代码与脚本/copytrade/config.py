"""配置与风控闸门.

所有参数来自环境变量，程序不硬编码任何凭证、不输出密钥。
D1~D3 采用方案推荐倾向：A1主+A2对账 / B2比例 / C3降级杠杆。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """零依赖加载 .env（KEY=VALUE，# 注释），只补空值不覆盖已设环境变量。

    修复原隐藏坑：Config.load() 此前只读 os.environ，从不读 .env 文件——
    用户把凭证写进 .env 也不生效。此处 read 时不输出任何值（凭证安全）。
    """
    if not path.exists():
        return
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:  # 环境已设优先，不覆盖
                os.environ[k] = v
    except OSError:
        pass


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
class AccountConfig:
    """单个子账户的凭证与分配资金（凭证只在内存，绝不输出）。"""

    name: str
    api_key: str
    api_secret: str
    allocated_capital: float

    def safe_name(self) -> str:
        """日志/DB 只显示账户名，不显示 key。"""
        return self.name


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

    # 数据源（2026-11-26 真实核验修正）
    # 关键：域名必须是 www.binance.com，不是 bapi.binance.com（后者整体 302 到首页）。
    # 参数名必须是 portfolioId，不是 uid；history 接口必须 POST JSON body。
    bapi_base: str = "https://www.binance.com"
    endpoint_order: str = "/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/order-history"
    endpoint_position: str = "/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/position-history"
    endpoint_detail: str = "/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/detail"
    proxy: str = ""  # 首选代理；留空走 requests 默认环境
    # 代理候选池：本机出口波动，单代理 502 时按序轮换（实测 7890 会间歇性 502）
    proxy_candidates: str = "http://127.0.0.1:7890"
    # 每轮拉取的最新页条数（热运行；冷启动用全量分页回填）
    page_size: int = 100
    # 冷启动全量回填最大页数（防无限翻页）
    bootstrap_max_pages: int = 20

    # 资金映射（D4 由用户定，默认纸面 1000 USDT）
    allocated_capital: float = 1000.0
    # D5 部署位置由用户定，代码只提供入口

    # 模式开关：paper 默认，live 必须显式开启且凭证齐全
    mode: str = "paper"  # paper | live
    api_key: str = ""       # 单账户回退（无 CT_ACCOUNT_N_* 时）
    api_secret: str = ""
    accounts: tuple[AccountConfig, ...] = ()  # 多账户：CT_ACCOUNT_1_* / CT_ACCOUNT_2_* ...

    # DB：仅用于本地/自建库；不连 prod。留空则用 SQLite 本地文件
    database_url: str = ""

    # 纸面回放：从 fixtures/*.json 读，不发网络请求
    replay_mode: bool = False
    replay_dir: str = "fixtures"

    risk: RiskGates = field(default_factory=RiskGates.from_env)
    health: HealthConfig = field(default_factory=HealthConfig.from_env)

    @staticmethod
    def _parse_accounts() -> tuple[AccountConfig, ...]:
        """从 CT_ACCOUNT_N_{NAME,API_KEY,API_SECRET,CAPITAL} 解析账户列表。

        N 从 1 连续递增，遇到缺 API_KEY 的序号即停止（容忍空洞前的间隔断点）。
        没有任何账户时返回空 tuple，回退到单账户 api_key/api_secret。
        """
        out: list[AccountConfig] = []
        i = 1
        while True:
            key = _env(f"CT_ACCOUNT_{i}_API_KEY")
            if not key:
                break
            secret = _env(f"CT_ACCOUNT_{i}_API_SECRET")
            name = _env(f"CT_ACCOUNT_{i}_NAME", f"sub{i}") or f"sub{i}"
            cap = _float(f"CT_ACCOUNT_{i}_CAPITAL", 1000.0)
            out.append(AccountConfig(name=name, api_key=key, api_secret=secret,
                                     allocated_capital=cap))
            i += 1
        return tuple(out)

    @classmethod
    def load(cls, base_dir: Path | None = None) -> "Config":
        # 关键：先加载 .env，否则用户填的凭证/账户列表全部读不到
        base = base_dir or Path(__file__).parent
        _load_dotenv(base / ".env")
        cfg = cls(
            leader_uid=_env("CT_LEADER_UID", "5075281354358777856"),
            proxy=_env("CT_PROXY", ""),
            proxy_candidates=_env("CT_PROXY_CANDIDATES", "http://127.0.0.1:7890"),
            page_size=_int("CT_PAGE_SIZE", 100),
            bootstrap_max_pages=_int("CT_BOOTSTRAP_MAX_PAGES", 20),
            allocated_capital=_float("CT_ALLOCATED_CAPITAL", 1000.0),
            mode=_env("CT_MODE", "paper").lower(),
            api_key=_env("CT_API_KEY", ""),
            api_secret=_env("CT_API_SECRET", ""),
            accounts=cls._parse_accounts(),
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
            accts = self.active_accounts
            if not accts:
                raise ValueError("live 模式必须提供子账户凭证 CT_ACCOUNT_N_*（或单账户 CT_API_KEY/SECRET）")
            for a in accts:
                if not a.api_key or not a.api_secret:
                    raise ValueError(f"账户 {a.name} 缺少 api_key/secret（用户自填，程序不输出）")
                if a.allocated_capital <= 0:
                    raise ValueError(f"账户 {a.name} allocated_capital 必须 > 0")
        if self.risk.max_leverage <= 0:
            raise ValueError("CT_MAX_LEVERAGE 必须 > 0")

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @property
    def active_accounts(self) -> tuple[AccountConfig, ...]:
        """实际参与跟单的账户：多账户优先，否则回退单账户。"""
        if self.accounts:
            return self.accounts
        if self.api_key and self.api_secret:
            return (AccountConfig(name="default", api_key=self.api_key,
                                  api_secret=self.api_secret,
                                  allocated_capital=self.allocated_capital),)
        return ()
