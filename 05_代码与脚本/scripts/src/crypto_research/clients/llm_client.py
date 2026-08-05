"""
LLM 客户端封装：优先使用火山方舟（豆包/DeepSeek），兜底使用 OpenAI 兼容接口。

支持火山方舟 Responses API（/api/v3/responses）和 OpenAI Chat Completions API 两种格式。
"""
from __future__ import annotations

import json
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from crypto_research.config import Settings


class LLMClient:
    """统一 LLM 调用接口。优先 ARK（火山方舟），其次 OpenAI 兼容。"""

    def __init__(self, settings: Settings, rpm: int = 60) -> None:
        self.settings = settings
        self._min_interval = 60.0 / rpm
        self._last_call: float = 0.0

        # 选择提供商
        self.provider: str = "none"
        self.api_key: str | None = None
        self.base_url: str | None = None
        self.model: str | None = None
        self.api_type: str = "chat"  # chat | responses

        if settings.ark_api_key and settings.ark_base_url and settings.ark_model:
            self.provider = "ark"
            self.api_key = settings.ark_api_key
            self.base_url = settings.ark_base_url.rstrip("/")
            self.model = settings.ark_model
            # deepseek 模型用 responses API，doubao 用 chat completions
            if "deepseek" in settings.ark_model.lower():
                self.api_type = "responses"
            else:
                self.api_type = "chat"
        elif settings.openai_api_key and settings.openai_base_url and settings.llm_model:
            self.provider = "openai"
            self.api_key = settings.openai_api_key
            self.base_url = settings.openai_base_url.rstrip("/")
            self.model = settings.llm_model
            self.api_type = "chat"

        self.session = requests.Session()
        self._last_raw_response: str = ""
        self._last_full_response: Any = None
        self._last_diag: dict = {}
        retry = Retry(
            total=3,
            backoff_factor=2,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["POST"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call = time.monotonic()

    def is_available(self) -> bool:
        return self.provider != "none"

    def chat(self, system_prompt: str, user_prompt: str,
             temperature: float = 0.1, max_tokens: int = 2048) -> str:
        """统一的聊天接口，自动选择底层 API 格式。"""
        self._last_diag = {
            "provider": self.provider,
            "model": self.model,
            "api_type": self.api_type,
            "base_url": self.base_url,
        }
        if self.api_type == "responses":
            result = self._call_responses(system_prompt, user_prompt, temperature, max_tokens)
        else:
            result = self._call_chat_completions(system_prompt, user_prompt, temperature, max_tokens)
        self._last_raw_response = result
        self._last_diag["result_len"] = len(result)
        return result

    def _call_chat_completions(
        self, system_prompt: str, user_prompt: str,
        temperature: float, max_tokens: int,
    ) -> str:
        """OpenAI 兼容的 /chat/completions 接口。"""
        if not self.is_available():
            raise RuntimeError("No LLM provider configured.")

        self._rate_limit()

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        resp = self.session.post(
            url, headers=headers, json=payload,
            timeout=self.settings.request_timeout_seconds,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    def _call_responses(
        self, system_prompt: str, user_prompt: str,
        temperature: float, max_tokens: int,
    ) -> str:
        """火山方舟 /api/v3/responses 接口（DeepSeek 系列模型）。"""
        if not self.is_available():
            raise RuntimeError("No LLM provider configured.")

        self._rate_limit()

        url = f"{self.base_url}/responses"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": self.model,
            "stream": False,
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
                {"role": "user", "content": [{"type": "input_text", "text": user_prompt}]},
            ],
        }

        resp = self.session.post(
            url, headers=headers, json=payload,
            timeout=self.settings.request_timeout_seconds,
        )
        resp.raise_for_status()
        data = resp.json()
        self._last_full_response = data  # 保存完整响应用于调试

        # 多种格式兼容提取
        # 格式1: output[].content[].text (type=output_text 或 type=text)
        output = data.get("output", [])
        for item in output:
            if item.get("type") == "message" and item.get("role") == "assistant":
                content = item.get("content", [])
                # content 可能是字符串
                if isinstance(content, str):
                    return content
                # content 可能是列表，尝试多种 type
                if isinstance(content, list):
                    parts = []
                    for c in content:
                        if isinstance(c, dict) and "text" in c:
                            parts.append(c["text"])
                    if parts:
                        return "".join(parts)

        # 格式2: 顶层 output_text
        if data.get("output_text"):
            return str(data["output_text"])

        # 格式3: choices (OpenAI 兼容格式)
        choices = data.get("choices", [])
        if choices:
            msg = choices[0].get("message", {})
            if msg.get("content"):
                return str(msg["content"])

        # 格式4: output 里直接是文本
        if isinstance(output, str) and output:
            return output

        # 兜底：返回空字符串（上层会当失败处理）
        self._last_diag = {
            "keys": list(data.keys()),
            "output_type": type(output).__name__,
            "output_len": len(output) if isinstance(output, list) else None,
            "first_item_keys": list(output[0].keys()) if isinstance(output, list) and output else None,
        }
        return ""

    def batch_check_crypto_relevance(
        self,
        items: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        """
        批量判断一批链接是否与加密货币投研相关。

        Args:
            items: [{id, url, title}] 列表

        Returns:
            [{id, relevant: bool, score: float, reason: str}] 列表
        """
        if not items:
            return []

        system_prompt = (
            "你是一个加密货币投研资料筛选专家。你的任务是判断给定的文档链接是否与"
            "加密货币（区块链、Web3、DeFi、NFT、代币经济学、智能合约、审计报告、"
            "白皮书等）投研直接相关。\n"
            "\n"
            "判断标准：\n"
            "- 相关（relevant=true）：项目白皮书、审计报告、代币经济学文档、"
            "DeFi 协议文档、链上数据分析、加密行业研究报告、Web3 技术文档等。\n"
            "- 无关（relevant=false）：普通学术论文、计算机通用技术文档、"
            "非加密行业报告、编程语言教程、普通企业官网、个人社交媒体等。\n"
            "\n"
            "只输出 JSON，不要输出其他内容。JSON 格式："
            '{results: [{id: string, relevant: boolean, score: 0.0-1.0, reason: "简短理由"}]}'
        )

        items_text = "\n".join(
            f'- id: {item["id"]}\n  url: {item["url"]}\n  title: {item.get("title", "")}'
            for item in items
        )
        user_prompt = f"请判断以下 {len(items)} 个链接的相关性：\n\n{items_text}"

        try:
            raw = self.chat(system_prompt, user_prompt, temperature=0.1, max_tokens=4096)
        except Exception as e:
            # 调用失败时全部默认保留（宁可留错，不可误删）
            return [
                {
                    "id": str(item["id"]),
                    "relevant": True,
                    "score": 0.5,
                    "reason": f"AI 调用失败: {str(e)[:100]}",
                }
                for item in items
            ]

        # 解析 JSON
        try:
            # 去除 markdown 代码块包裹（```json ... ``` 或 ``` ... ```）
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                # 去掉开头的 ```json 或 ```
                first_line_end = cleaned.find("\n")
                if first_line_end > 0:
                    cleaned = cleaned[first_line_end + 1:]
                # 去掉结尾的 ```
                if cleaned.endswith("```"):
                    cleaned = cleaned[:-3].strip()

            data = json.loads(cleaned)
            results = data.get("results", [])
            if not isinstance(results, list):
                raise ValueError(f"results 不是列表，类型: {type(results).__name__}")
            result_map = {}
            for r in results:
                rid = r.get("id")
                if rid is not None:
                    # 统一转成字符串，避免 int/str 类型不匹配
                    result_map[str(rid)] = {
                        "id": str(rid),
                        "relevant": bool(r.get("relevant", False)),
                        "score": float(r.get("score", 0.0)),
                        "reason": str(r.get("reason", ""))[:200],
                    }
            return [
                result_map.get(
                    str(item["id"]),
                    {
                        "id": str(item["id"]),
                        "relevant": True,
                        "score": 0.5,
                        "reason": "未匹配到AI结果，默认保留",
                    },
                )
                for item in items
            ]
        except (json.JSONDecodeError, ValueError) as e:
            # 记录解析失败的原始文本前 200 字符
            self._last_diag["parse_error"] = str(e)
            self._last_diag["parse_raw_prefix"] = raw[:200]
            return [
                {
                    "id": str(item["id"]),
                    "relevant": True,
                    "score": 0.5,
                    "reason": f"AI响应解析失败: {str(e)[:100]}",
                }
                for item in items
            ]
