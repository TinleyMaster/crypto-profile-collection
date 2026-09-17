from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    cmc_api_key: str
    database_url: str | None = None
    cmc_base_url: str = "https://pro-api.coinmarketcap.com"
    coingecko_base_url: str = "https://api.coingecko.com/api/v3"
    coingecko_api_key: str | None = None
    coingecko_api_keys: list[str] | None = None  # 多 key 轮替
    defillama_base_url: str = "https://api.llama.fi"
    etherscan_api_key: str | None = None
    bscscan_api_key: str | None = None
    binplorer_api_key: str | None = None
    github_token: str | None = None
    # Solana 链上采集（Helius RPC，免费档即可满足持仓 Top20 / 转账监控）
    helius_api_key: str | None = None
    # Tron 链上采集（TronGrid，免费档即可）
    trongrid_api_key: str | None = None
    # TON 链上采集（TON Center，免费档即可）
    toncenter_api_key: str | None = None
    # LLM (OpenAI 兼容)
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    llm_model: str | None = None
    # 豆包 / 火山方舟 (OpenAI 兼容)
    ark_api_key: str | None = None
    ark_base_url: str | None = None
    ark_model: str | None = None
    request_timeout_seconds: int = 30
    # 网络代理（enrich 爬取区块浏览器标签等外部 HTTP 时使用；requests 也读 HTTPS_PROXY 环境变量）
    https_proxy: str | None = None
    http_proxy: str | None = None
    # CryptoETF (cryptoetf.today) ETF 资金流 API
    cryptoetf_api_key: str | None = None
    cryptoetf_base_url: str = "https://api.cryptoetf.today/api/v1"
    # Firecrawl Web Search（用于 AI 信号分析时补全缺失数据维度）
    firecrawl_api_key: str | None = None
    firecrawl_base_url: str = "https://api.firecrawl.dev"
    # 邮件通知（解锁追踪提醒）
    smtp_host: str | None = None
    smtp_port: int = 465
    smtp_user: str | None = None
    smtp_pass: str | None = None
    smtp_to: str | None = None
    smtp_from: str | None = None
    # 系统维护告警专用收件人（仅发管理员）
    admin_email: str | None = None
    # ── KOL 信号自动交易（币胜操盘手日记 → 币安子账户）──
    # IMAP 收信（默认复用 SMTP_USER / SMTP_PASS，QQ 授权码通用）
    imap_host: str = "imap.qq.com"
    imap_port: int = 993
    imap_user: str | None = None
    imap_pass: str | None = None
    # 币安子账户合约 API（只开「合约交易」权限，不开提现）
    binance_api_key: str | None = None
    binance_api_secret: str | None = None
    binance_fapi_base_url: str = "https://fapi.binance.com"
    # 风控参数
    signal_trade_enabled: bool = False          # 总开关，=1 才真正下单
    signal_max_notional_usdt: float = 300.0     # 单笔最大名义价值（USDT）
    signal_leverage: int = 5                    # 开仓杠杆
    signal_order_type: str = "limit"            # limit=挂单进场价 / market=市价
    signal_max_price_deviation_pct: float = 1.5 # 现价偏离进场价超过该比例则跳过（%）
    signal_cooldown_minutes: int = 30           # 同一币种两次开单最小间隔（分钟）
    signal_stop_loss_pct: float = 0.0           # 固定止损比例（0=不设；AI 关闭时生效）
    signal_take_profit_pct: float = 0.0         # 固定止盈比例（0=不设；AI 关闭时生效）
    # AI 止损止盈（AI 建议 + 2% 总资金硬顶 + 兜底）
    signal_ai_sltp_enabled: bool = True         # 是否启用 AI 止损止盈（需已配 LLM Key）
    signal_max_loss_pct_of_equity: float = 2.0  # 单笔最大亏损占总权益的百分比（硬顶）
    signal_fallback_stop_loss_pct: float = 1.5  # AI 失败时的兜底止损距离（%）
    signal_atr_multiplier: float = 2.0          # 止损机械底线 = ATR 倍数 × ATR%（防噪音扫损）
    # DB 驱动（读 biz.kol_signal，等 AI 分类确认为实时喊单 post_type='prediction' 再开单）
    signal_kol_whitelist: list[str] = None      # 允许自动开单的 KOL 昵称白名单（空=全部 kol 类博主）
    signal_min_confidence: float = 0.8          # 信号最低置信度（AI 分类）
    signal_max_age_hours: float = 6.0           # 只处理发帖后 N 小时内的信号（防陈旧单）

    def get_coingecko_keys(self) -> list[str]:
        """返回所有可用的 CoinGecko API key（单个或多个），无 key 返回空列表。"""
        if self.coingecko_api_keys:
            return list(self.coingecko_api_keys)
        if self.coingecko_api_key:
            return [self.coingecko_api_key]
        return []


def load_local_env_file() -> None:
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _parse_coingecko_keys() -> list[str]:
    """解析 COINGECKO_API_KEY 中的多个 key（逗号分隔），返回全部 key 列表。"""
    raw = os.getenv("COINGECKO_API_KEY", "").strip()
    if not raw:
        return []
    return [k.strip() for k in raw.split(",") if k.strip()]


def get_settings(require_database: bool = True) -> Settings:
    load_local_env_file()
    cmc_api_key = os.getenv("CMC_API_KEY", "").strip()
    database_url = os.getenv("DATABASE_URL", "").strip()

    if not cmc_api_key:
        raise RuntimeError("Missing required environment variable: CMC_API_KEY")
    if require_database and not database_url:
        raise RuntimeError("Missing required environment variable: DATABASE_URL")

    cg_keys = _parse_coingecko_keys()

    return Settings(
        cmc_api_key=cmc_api_key,
        database_url=database_url or None,
        coingecko_api_key=cg_keys[0] if cg_keys else None,
        coingecko_api_keys=cg_keys if len(cg_keys) > 1 else None,
        etherscan_api_key=os.getenv("ETHERSCAN_API_KEY", "").strip() or None,
        bscscan_api_key=os.getenv("BSCSCAN_API_KEY", "").strip() or None,
        binplorer_api_key=os.getenv("BINPLORER_API_KEY", "").strip() or None,
        github_token=os.getenv("GITHUB_TOKEN", "").strip() or None,
        helius_api_key=os.getenv("HELIUS_API_KEY", "").strip() or None,
        trongrid_api_key=os.getenv("TRONGRID_API_KEY", "").strip() or None,
        toncenter_api_key=os.getenv("TONCENTER_API_KEY", "").strip() or None,
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip() or None,
        openai_base_url=os.getenv("OPENAI_BASE_URL", "").strip() or None,
        llm_model=os.getenv("LLM_MODEL", "").strip() or None,
        ark_api_key=os.getenv("ARK_API_KEY", "").strip() or None,
        ark_base_url=os.getenv("ARK_BASE_URL", "").strip() or None,
        ark_model=os.getenv("ARK_MODEL", "").strip() or None,
        https_proxy=os.getenv("HTTPS_PROXY", "").strip() or os.getenv("https_proxy", "").strip() or None,
        http_proxy=os.getenv("HTTP_PROXY", "").strip() or os.getenv("http_proxy", "").strip() or None,
        cryptoetf_api_key=os.getenv("CRYPTOETF_KEY", "").strip() or None,
        cryptoetf_base_url=os.getenv("CRYPTOETF_BASE", "https://api.cryptoetf.today/api/v1").strip(),
        firecrawl_api_key=os.getenv("FIRECRAWL_API_KEY", "").strip() or None,
        firecrawl_base_url=os.getenv("FIRECRAWL_BASE_URL", "https://api.firecrawl.dev").strip(),
        smtp_host=os.getenv("SMTP_HOST", "").strip() or None,
        smtp_port=int(os.getenv("SMTP_PORT", "465") or 465),
        smtp_user=os.getenv("SMTP_USER", "").strip() or None,
        smtp_pass=os.getenv("SMTP_PASS", "").strip() or None,
        smtp_to=os.getenv("SMTP_TO", "").strip() or None,
        smtp_from=os.getenv("SMTP_FROM", "").strip() or None,
        admin_email=os.getenv("ADMIN_EMAIL", "").strip() or None,
        imap_host=os.getenv("IMAP_HOST", "imap.qq.com").strip() or "imap.qq.com",
        imap_port=int(os.getenv("IMAP_PORT", "993") or 993),
        imap_user=os.getenv("IMAP_USER", "").strip() or None,
        imap_pass=os.getenv("IMAP_PASS", "").strip() or None,
        binance_api_key=os.getenv("BINANCE_API_KEY", "").strip() or None,
        binance_api_secret=os.getenv("BINANCE_API_SECRET", "").strip() or None,
        binance_fapi_base_url=os.getenv("BINANCE_FAPI_BASE_URL", "https://fapi.binance.com").strip(),
        signal_trade_enabled=os.getenv("SIGNAL_TRADE_ENABLED", "0").strip() in ("1", "true", "True"),
        signal_max_notional_usdt=float(os.getenv("SIGNAL_MAX_NOTIONAL_USDT", "300") or 300),
        signal_leverage=int(os.getenv("SIGNAL_LEVERAGE", "5") or 5),
        signal_order_type=os.getenv("SIGNAL_ORDER_TYPE", "limit").strip().lower() or "limit",
        signal_max_price_deviation_pct=float(os.getenv("SIGNAL_MAX_PRICE_DEVIATION_PCT", "1.5") or 1.5),
        signal_cooldown_minutes=int(os.getenv("SIGNAL_COOLDOWN_MINUTES", "30") or 30),
        signal_stop_loss_pct=float(os.getenv("SIGNAL_STOP_LOSS_PCT", "0") or 0),
        signal_take_profit_pct=float(os.getenv("SIGNAL_TAKE_PROFIT_PCT", "0") or 0),
        signal_ai_sltp_enabled=os.getenv("SIGNAL_AI_SLTP_ENABLED", "1").strip() in ("1", "true", "True"),
        signal_max_loss_pct_of_equity=float(os.getenv("SIGNAL_MAX_LOSS_PCT_OF_EQUITY", "2") or 2),
        signal_fallback_stop_loss_pct=float(os.getenv("SIGNAL_FALLBACK_STOP_LOSS_PCT", "1.5") or 1.5),
        signal_atr_multiplier=float(os.getenv("SIGNAL_ATR_MULTIPLIER", "2") or 2),
        signal_kol_whitelist=[n.strip() for n in os.getenv("SIGNAL_KOL_WHITELIST", "").split(",") if n.strip()] or None,
        signal_min_confidence=float(os.getenv("SIGNAL_MIN_CONFIDENCE", "0.8") or 0.8),
        signal_max_age_hours=float(os.getenv("SIGNAL_MAX_AGE_HOURS", "6") or 6),
    )
