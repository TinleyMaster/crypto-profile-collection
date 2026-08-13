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
    github_token: str | None = None
    # LLM (OpenAI 兼容)
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    llm_model: str | None = None
    # 豆包 / 火山方舟 (OpenAI 兼容)
    ark_api_key: str | None = None
    ark_base_url: str | None = None
    ark_model: str | None = None
    request_timeout_seconds: int = 30
    # 邮件通知（解锁追踪提醒）
    smtp_host: str | None = None
    smtp_port: int = 465
    smtp_user: str | None = None
    smtp_pass: str | None = None
    smtp_to: str | None = None
    smtp_from: str | None = None


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


def _parse_coingecko_keys() -> list[str] | None:
    """解析 COINGECKO_API_KEY 中的多个 key（逗号分隔）。"""
    raw = os.getenv("COINGECKO_API_KEY", "").strip()
    if not raw:
        return None
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    return keys if len(keys) > 1 else None  # 只有一个 key 时不需要轮替列表


def get_settings(require_database: bool = True) -> Settings:
    load_local_env_file()
    cmc_api_key = os.getenv("CMC_API_KEY", "").strip()
    database_url = os.getenv("DATABASE_URL", "").strip()

    if not cmc_api_key:
        raise RuntimeError("Missing required environment variable: CMC_API_KEY")
    if require_database and not database_url:
        raise RuntimeError("Missing required environment variable: DATABASE_URL")

    return Settings(
        cmc_api_key=cmc_api_key,
        database_url=database_url or None,
        coingecko_api_key=os.getenv("COINGECKO_API_KEY", "").strip() or None,
        coingecko_api_keys=_parse_coingecko_keys(),
        etherscan_api_key=os.getenv("ETHERSCAN_API_KEY", "").strip() or None,
        bscscan_api_key=os.getenv("BSCSCAN_API_KEY", "").strip() or None,
        github_token=os.getenv("GITHUB_TOKEN", "").strip() or None,
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip() or None,
        openai_base_url=os.getenv("OPENAI_BASE_URL", "").strip() or None,
        llm_model=os.getenv("LLM_MODEL", "").strip() or None,
        ark_api_key=os.getenv("ARK_API_KEY", "").strip() or None,
        ark_base_url=os.getenv("ARK_BASE_URL", "").strip() or None,
        ark_model=os.getenv("ARK_MODEL", "").strip() or None,
        smtp_host=os.getenv("SMTP_HOST", "").strip() or None,
        smtp_port=int(os.getenv("SMTP_PORT", "465") or 465),
        smtp_user=os.getenv("SMTP_USER", "").strip() or None,
        smtp_pass=os.getenv("SMTP_PASS", "").strip() or None,
        smtp_to=os.getenv("SMTP_TO", "").strip() or None,
        smtp_from=os.getenv("SMTP_FROM", "").strip() or None,
    )
