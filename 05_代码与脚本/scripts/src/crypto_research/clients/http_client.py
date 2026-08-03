from __future__ import annotations

from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class SimpleHttpClient:
    def __init__(self, timeout_seconds: int = 20) -> None:
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update(
            {
                "User-Agent": "crypto-research-doc-discovery/1.0",
                "Accept": "*/*",
            }
        )
        retry = Retry(
            total=2,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def probe(self, url: str) -> dict[str, Any]:
        try:
            response = self.session.head(
                url,
                allow_redirects=True,
                timeout=self.timeout_seconds,
            )
            content_type = response.headers.get("Content-Type")
            content_length = response.headers.get("Content-Length")
            return {
                "ok": True,
                "status_code": response.status_code,
                "final_url": response.url,
                "content_type": content_type,
                "content_length": content_length,
                "method": "HEAD",
            }
        except requests.RequestException:
            try:
                response = self.session.get(
                    url,
                    allow_redirects=True,
                    timeout=self.timeout_seconds,
                    stream=True,
                )
                content_type = response.headers.get("Content-Type")
                content_length = response.headers.get("Content-Length")
                response.close()
            except requests.RequestException as exc:
                return {
                    "ok": False,
                    "status_code": None,
                    "final_url": url,
                    "content_type": None,
                    "content_length": None,
                    "method": "GET",
                    "error": str(exc),
                }
            return {
                "ok": True,
                "status_code": response.status_code,
                "final_url": response.url,
                "content_type": content_type,
                "content_length": content_length,
                "method": "GET",
            }
