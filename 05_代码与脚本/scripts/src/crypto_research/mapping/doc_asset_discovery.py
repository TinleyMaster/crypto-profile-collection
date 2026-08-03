from __future__ import annotations

from urllib.parse import urlparse


DIRECT_DOC_HINTS = (
    ".pdf",
    "whitepaper",
    "litepaper",
    "tokenomics",
    "audit",
    "deck",
    "paper",
    "docs/",
)


def looks_like_pdf_url(url: str) -> bool:
    lowered = (url or "").lower()
    return lowered.endswith(".pdf") or ".pdf?" in lowered or "/pdf/" in lowered


def should_probe_direct_asset(entry_type: str, url: str) -> bool:
    lowered = (url or "").lower()

    if looks_like_pdf_url(lowered):
        return True

    if entry_type == "docs":
        return True

    if entry_type == "official_website":
        return any(hint in lowered for hint in DIRECT_DOC_HINTS)

    if entry_type in {"github", "medium"}:
        return any(hint in lowered for hint in DIRECT_DOC_HINTS)

    return any(hint in lowered for hint in DIRECT_DOC_HINTS)


def infer_doc_type(entry_type: str, url: str) -> str:
    lowered = (url or "").lower()
    normalized = lowered.replace("-", " ").replace("_", " ")
    if "whitepaper" in lowered or "white paper" in normalized:
        return "whitepaper"
    if "litepaper" in lowered or "lite paper" in normalized:
        return "whitepaper"
    if "audit" in lowered:
        return "audit"
    if "tokenomics" in lowered:
        return "tokenomics"
    if entry_type == "docs":
        return "docs"
    if entry_type == "announcement":
        return "announcement"
    return "other"


def extract_file_name(url: str) -> str | None:
    path = urlparse(url).path
    if not path:
        return None
    name = path.rsplit("/", 1)[-1].strip()
    return name or None
