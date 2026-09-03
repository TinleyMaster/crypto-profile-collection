"""
LLM 客户端封装：优先使用火山方舟（豆包/DeepSeek），兜底使用 OpenAI 兼容接口。

支持火山方舟 Responses API（/api/v3/responses）和 OpenAI Chat Completions API 两种格式。
主 provider 调用失败（额度 402/限流/5xx 等）时自动切换兜底 provider 重试一次。
"""
from __future__ import annotations

import json
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.exceptions import ReadTimeoutError
from urllib3.util.retry import Retry

from crypto_research.config import Settings
from crypto_research.mapping.taxonomy import CONTENT_TOPICS


def extract_json_from_llm_response(raw: str) -> Any:
    """从 LLM 返回内容中健壮地提取 JSON。

    处理多种情况：
    - 纯 JSON
    - markdown 代码块包裹（```json ... ``` 或 ``` ... ```）
    - JSON 前后有说明文字（从第一个 { 到最后一个 } 提取）
    - 被截断的 JSON（max_tokens 不足导致末尾不完整）
    - 空内容
    """
    if not raw or not raw.strip():
        raise ValueError("LLM 返回空内容")

    text = raw.strip()

    # 策略1：去除 markdown 代码块后解析
    cleaned = text
    if cleaned.startswith("```"):
        first_line_end = cleaned.find("\n")
        if first_line_end > 0:
            cleaned = cleaned[first_line_end + 1:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # 策略2：在文本中查找 JSON 对象（{ 到 }）
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    # 策略3：查找 markdown 代码块中的 JSON（可能有前置文字）
    fence_start = text.find("```")
    if fence_start >= 0:
        fence_end = text.find("\n", fence_start)
        if fence_end > 0:
            inner = text[fence_end + 1:]
            close_fence = inner.rfind("```")
            if close_fence > 0:
                inner_cleaned = inner[:close_fence].strip()
                try:
                    return json.loads(inner_cleaned)
                except json.JSONDecodeError:
                    pass

    # 策略4：处理被截断的 JSON（max_tokens 不足导致末尾不完整）
    # 从第一个 { 开始，尝试在最后一个完整键值对处截断并补上 }
    if start >= 0:
        candidate = text[start:]
        # 找到最后一个逗号（键值对分隔符），去掉不完整的后半部分，补 }
        last_comma = candidate.rfind(",")
        if last_comma > 0:
            try:
                fixed = candidate[:last_comma] + "}"
                return json.loads(fixed)
            except json.JSONDecodeError:
                pass
        # 如果逗号方案失败，尝试在最后一个完整值（} 或 ] 或 "）处截断
        for ch in ('"}', ']', '"'):
            pos = candidate.rfind(ch)
            if pos > 0:
                try:
                    return json.loads(candidate[:pos + len(ch)])
                except json.JSONDecodeError:
                    continue

    raise ValueError(f"无法解析 LLM 返回的 JSON，前 200 字符: {text[:200]}")


class LLMClient:
    """统一 LLM 调用接口。优先 ARK（火山方舟），其次 OpenAI 兼容（DeepSeek 直连等），
    主 provider 失败时自动切换兜底 provider 重试一次。"""

    @staticmethod
    def _normalize_base_url(url: str) -> str:
        """去掉 base_url 上可能已带的接口后缀，避免拼接时重复
        （如 .../api/v3/responses 会再拼 /responses 变成 /responses/responses → 404）。"""
        url = (url or "").rstrip("/")
        for suffix in ("/responses", "/chat/completions"):
            if url.endswith(suffix):
                url = url[: -len(suffix)].rstrip("/")
                break
        return url

    def __init__(self, settings: Settings, rpm: int = 60, timeout: int | None = None) -> None:
        self.settings = settings
        self._min_interval = 60.0 / rpm
        self._last_call: float = 0.0
        self._timeout = timeout or settings.request_timeout_seconds

        # 选择提供商：ARK 优先，OpenAI 兼容（DeepSeek 等）兜底
        self._provider_list: list[dict[str, Any]] = []
        if settings.ark_api_key and settings.ark_base_url and settings.ark_model:
            self._provider_list.append({
                "name": "ark",
                "api_key": settings.ark_api_key,
                "base_url": self._normalize_base_url(settings.ark_base_url),
                "model": settings.ark_model,
                # deepseek 模型用 responses API，doubao 用 chat completions
                "api_type": "responses" if "deepseek" in settings.ark_model.lower() else "chat",
            })
        if settings.openai_api_key and settings.openai_base_url and settings.llm_model:
            self._provider_list.append({
                "name": "openai",
                "api_key": settings.openai_api_key,
                "base_url": self._normalize_base_url(settings.openai_base_url),
                "model": settings.llm_model,
                "api_type": "chat",
            })

        self.provider: str = "none"
        self.api_key: str | None = None
        self.base_url: str | None = None
        self.model: str | None = None
        self.api_type: str = "chat"  # chat | responses
        self._fallback_provider: dict[str, Any] | None = None
        if self._provider_list:
            self._apply_provider(self._provider_list[0])
            if len(self._provider_list) > 1:
                self._fallback_provider = self._provider_list[1]

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

    def _apply_provider(self, cfg: dict[str, Any]) -> None:
        self.provider = cfg["name"]
        self.api_key = cfg["api_key"]
        self.base_url = cfg["base_url"]
        self.model = cfg["model"]
        self.api_type = cfg["api_type"]

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call = time.monotonic()

    def is_available(self) -> bool:
        return self.provider != "none"

    def chat(self, system_prompt: str, user_prompt: str,
             temperature: float = 0.1, max_tokens: int = 2048,
             timeout_retries: int = 3) -> str:
        """统一的聊天接口，自动选择底层 API 格式；主 provider 失败时切换兜底重试一次。

        timeout_retries: ReadTimeoutError 重试次数（指数退避），默认 3。
        """
        self._last_diag = {
            "provider": self.provider,
            "model": self.model,
            "api_type": self.api_type,
            "base_url": self.base_url,
        }
        last_exc = None
        for attempt in range(timeout_retries):
            try:
                result = self._dispatch(system_prompt, user_prompt, temperature, max_tokens)
                self._last_raw_response = result
                self._last_diag["result_len"] = len(result)
                return result
            except ReadTimeoutError as e:
                last_exc = e
                if attempt < timeout_retries - 1:
                    wait = 2 ** attempt * 5  # 5s, 10s, 20s
                    self._last_diag["timeout_retry"] = attempt + 1
                    import logging
                    logging.getLogger(__name__).warning(
                        "LLM ReadTimeout (attempt %d/%d), retrying in %ds: %s",
                        attempt + 1, timeout_retries, wait, e,
                    )
                    time.sleep(wait)
                    continue
                # 最后一次尝试失败，走兜底逻辑
                break
            except Exception as exc:
                last_exc = exc
                break

        # 主 provider 失败（额度 402 / 限流 / 5xx / 超时等），切换兜底 provider 重试一次
        exc = last_exc
        if self._fallback_provider:
            fallback = self._fallback_provider
            self._fallback_provider = None  # 只兜底一次，避免死循环
            self._apply_provider(fallback)
            self._last_diag = {
                "provider": self.provider,
                "model": self.model,
                "api_type": self.api_type,
                "base_url": self.base_url,
                "fallback_from": str(exc),
            }
            try:
                result = self._dispatch(system_prompt, user_prompt, temperature, max_tokens)
                self._last_raw_response = result
                self._last_diag["result_len"] = len(result)
                return result
            except Exception as exc2:
                self._apply_provider(self._provider_list[0])
                raise exc2
        else:
            raise exc

    def _dispatch(self, system_prompt: str, user_prompt: str,
                  temperature: float, max_tokens: int) -> str:
        if self.api_type == "responses":
            return self._call_responses(system_prompt, user_prompt, temperature, max_tokens)
        return self._call_chat_completions(system_prompt, user_prompt, temperature, max_tokens)

    def _call_chat_completions(
        self, system_prompt: str, user_prompt: str,
        temperature: float, max_tokens: int,
    ) -> str:
        """OpenAI 兼容的 /chat/completions 接口（流式）。"""
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
            "max_tokens": max_tokens,
            "stream": True,
        }

        # DeepSeek V4 默认启用思考模式，噪声判断不需要深度推理，显式禁用
        # 避免 max_tokens 被思维链吃掉导致 content 为空
        if "deepseek" in (self.model or "").lower():
            payload["thinking"] = {"type": "disabled"}
        else:
            payload["temperature"] = temperature

        # 流式：connect=10s, read=60s（字节间隔）；流式下持续有 SSE chunk → 不触发 read timeout
        resp = self.session.post(
            url, headers=headers, json=payload,
            timeout=(10, 60), stream=True,
        )
        resp.raise_for_status()

        content = ""
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            chunk = line[len("data:"):].strip()
            if chunk == "[DONE]":
                break
            try:
                data = json.loads(chunk)
                delta = data["choices"][0]["delta"].get("content") or ""
                content += delta
            except Exception:
                continue

        self._last_full_response = {"streamed_content_length": len(content)}
        return content

    def _call_responses(
        self, system_prompt: str, user_prompt: str,
        temperature: float, max_tokens: int,
    ) -> str:
        """火山方舟 /api/v3/responses 接口（DeepSeek 系列模型，流式）。"""
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
            "stream": True,
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
                {"role": "user", "content": [{"type": "input_text", "text": user_prompt}]},
            ],
        }

        # 流式：connect=10s, read=60s（字节间隔）；流式下持续有 SSE chunk → 不触发 read timeout
        resp = self.session.post(
            url, headers=headers, json=payload,
            timeout=(10, 60), stream=True,
        )
        resp.raise_for_status()

        # 消费 SSE，按 Responses API 格式重建 content
        content = ""
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            chunk = line[len("data:"):].strip()
            if chunk == "[DONE]":
                break
            try:
                data = json.loads(chunk)
                # 格式1: output_message item，delta.content 为 text
                if data.get("type") == "output_message":
                    for c in data.get("content", []):
                        if isinstance(c, dict) and c.get("type") == "output_text":
                            content += c.get("text", "")
                # 格式2: choices delta（部分 provider 用此格式）
                elif "choices" in data:
                    delta = data["choices"][0].get("delta", {})
                    content += delta.get("content") or ""
            except Exception:
                continue

        self._last_full_response = {"streamed_content_length": len(content)}
        return content

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
            "重要提示：标题中的 [SYMBOL NAME] 前缀表示该链接所属的加密资产项目。"
            "如果链接来自知名审计公司（chainsecurity、certik、hacken、peckshield、"
            "quillaudits、trailofbits、openzeppelin、zokyo 等）且审计内容与所属资产相关，"
            "应判定为相关（这是该项目的审计报告，属于投研资料）。\n"
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
            data = extract_json_from_llm_response(raw)
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

    def batch_check_asset_noise(
        self,
        asset_symbol: str,
        asset_name: str,
        domain_groups: list[dict[str, Any]],
        asset_id: int = 0,
    ) -> list[dict[str, Any]]:
        """
        按资产上下文判断域名/链接是否为噪声。

        与 batch_check_crypto_relevance 的区别：
        - 按资产分组，AI 能看到同一资产的所有域名，判断更准确
        - 按域名粒度判断（而非逐条 URL），效率高很多

        Args:
            asset_symbol: 代币符号
            asset_name: 代币名称
            domain_groups: [{domain, count, sample_urls: [url, ...], entry_ids: [id, ...]}]
            asset_id: 资产 ID（仅用于日志）

        Returns:
            [{domain, decision: "noise"|"relevant"|"uncertain", reason: str,
              affected_ids: [id, ...], sample_urls: [url, ...]}]
        """
        if not domain_groups:
            return []

        system_prompt = (
            "你是一个加密货币投研资料筛选专家。你的任务是：给定一个特定加密资产，"
            "判断其文档链接中哪些域名与加密/Web3 投研相关，哪些是噪声，哪些不确定。\n"
            "\n"
            "判断标准：\n"
            "- 相关（decision=relevant）：以下类型的链接都应保留，因为它们对投研有价值：\n"
            "  1. 项目官方文档（白皮书、代币经济学、治理文档、路线图等）\n"
            "  2. 审计报告和安全评估（即使来自第三方审计平台，如 tech-audit.org, "
            "quillaudits, hacken, certik, certora, openzeppelin 等）\n"
            "  3. 社交平台链接（Twitter/X, Telegram, Discord, LinkedIn, Reddit, "
            "Medium 等）—— 这些包含团队信息、社区动态、官方公告，是投研核心资料\n"
            "  4. 合作伙伴/生态页面（如 partners.circle.com 等）\n"
            "  5. 加密行业通用平台（CoinGecko, CoinMarketCap, DeFiLlama, Dune 等）\n"
            "  6. GitHub 仓库（即使是第三方审计仓库，只要涉及加密项目审计）\n"
            "- 噪声（decision=noise）：仅以下类型应判定为噪声：\n"
            "  1. 非加密学术论文（arxiv, springer, neurips, researchgate 等）\n"
            "  2. 通用编程/技术文档（npm, pip, nuget, packagist, ubuntu packages, "
            "laravel, ruby, postgresql, elastic, docker 等）\n"
            "  3. 与该资产完全无关的其他项目专属文档\n"
            "  4. 电商/企业网站（amazon, dropbox, manageengine 等）\n"
            "  5. 通用工具/聚合网站（非加密类，如 digitalocean, powershellgallery 等）\n"
            "- 不确定（decision=uncertain）：仅凭域名和样本 URL 无法判断时标记为 uncertain，"
            "程序会抓取页面正文后再二次判断。\n"
            "\n"
            "⚠️ 重要原则：宁可保留，不可误删。无法确定时优先判定为 relevant；"
            "只有当你认为必须读取页面正文才能确认时，才判定为 uncertain。\n"
            "\n"
            "⚠️ 密度预警：\n"
            "- 如果一个域名在单个资产下链接数超过 100 条，且占该资产总链接数的 90% 以上，"
            "极有可能是**应用类网站被误爬**（如会计平台、内部管理系统、无分页的 Web 应用），"
            "应判定为噪声（decision=noise）。\n"
            "- 正常文档站即使内容多，链接数通常也不会超过几百条，且会分散在多个域名下。\n"
            "\n"
            "只输出 JSON，不要输出其他内容。JSON 格式：\n"
            '{"results": [{"domain": "string", "decision": "noise|relevant|uncertain", '
            '"reason": "简短理由"}]}'
        )

        domains_text_parts = []
        for g in domain_groups:
            domain = g["domain"]
            count = g["count"]
            samples = g.get("sample_urls", [])[:3]
            samples_str = "\n    ".join(samples)
            domains_text_parts.append(
                f"- {domain}: {count} 条链接\n"
                f"  样本:\n    {samples_str}"
            )
        domains_text = "\n".join(domains_text_parts)

        user_prompt = (
            f"资产: {asset_symbol} ({asset_name})\n"
            f"asset_id: {asset_id}\n\n"
            f"该资产在 deep_crawl 中发现的域名及链接数：\n\n"
            f"{domains_text}\n\n"
            f"请判断以上 {len(domain_groups)} 个域名，哪些是噪声、哪些相关、哪些不确定。"
        )

        try:
            raw = self.chat(system_prompt, user_prompt, temperature=0.1, max_tokens=4096)
        except Exception as e:
            return [
                {"domain": g["domain"], "decision": "relevant", "reason": f"AI调用失败: {e}",
                 "affected_ids": g.get("entry_ids", []), "sample_urls": g.get("sample_urls", [])}
                for g in domain_groups
            ]

        # 解析 JSON
        try:
            data = extract_json_from_llm_response(raw)
            results = data.get("results", [])
            if not isinstance(results, list):
                raise ValueError(f"results 不是列表，类型: {type(results).__name__}")

            # 构建 domain → 结果映射
            result_map = {}
            for r in results:
                domain = r.get("domain", "")
                if not domain:
                    continue
                decision = str(r.get("decision", "relevant")).lower()
                if decision not in ("noise", "relevant", "uncertain"):
                    decision = "relevant"
                result_map[domain] = {
                    "decision": decision,
                    "reason": str(r.get("reason", ""))[:200],
                }

            return [
                {
                    "domain": g["domain"],
                    "decision": result_map.get(g["domain"], {}).get("decision", "relevant"),
                    "reason": result_map.get(g["domain"], {}).get("reason", "未匹配到AI结果"),
                    "affected_ids": g.get("entry_ids", []),
                    "sample_urls": g.get("sample_urls", []),
                }
                for g in domain_groups
            ]
        except (json.JSONDecodeError, ValueError) as e:
            # 解析失败时全部默认保留
            return [
                {"domain": g["domain"], "decision": "relevant", "reason": f"AI响应解析失败: {e}",
                 "affected_ids": g.get("entry_ids", []), "sample_urls": g.get("sample_urls", [])}
                for g in domain_groups
            ]

    def judge_audit_links(
        self,
        asset_symbol: str,
        asset_name: str,
        asset_description: str,
        links: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        审计聚合链接按条判断：仅保留属于当前代币的审计资料。

        Args:
            links: [{entry_id, url}]

        Returns:
            [{entry_id, url, keep: bool, reason: str}]
        """
        if not links:
            return []

        system_prompt = (
            "你是一个加密货币项目审计报告筛选专家。给定一个代币的基础资料，"
            "以及若干来自审计/安全平台（可能聚合了多个项目）的链接，"
            "判断每条链接是否属于该代币自己的审计资料。\n"
            "\n"
            "判断标准：\n"
            "- keep=true（保留）：链接的 URL 明确指向该代币项目，"
            "或内容是该项目的审计报告、安全评估、漏洞披露。\n"
            "- keep=false（删除）：链接指向其他项目、其他代币，"
            "或与该代币完全无关的审计/安全内容。\n"
            "\n"
            "⚠️ 原则：只保留「当前代币」的审计资料，其他项目的一律删除。"
            "尽量依据 URL 中的项目标识（符号/名称）判断归属；"
            "如果 URL 中不含该代币标识且无法确认归属，判定为删除。\n"
            "\n"
            "只输出 JSON，不要输出其他内容。JSON 格式：\n"
            '{"results": [{"url": "string", "keep": true/false, "reason": "简短理由"}]}'
        )

        links_text = "\n".join(
            f'- id: {l["entry_id"]}\n  url: {l["url"]}' for l in links
        )
        desc = (asset_description or "").strip()[:500]
        user_prompt = (
            f"代币基础资料：\n"
            f"- 符号: {asset_symbol}\n"
            f"- 名称: {asset_name}\n"
            f"- 简介: {desc or '（无）'}\n\n"
            f"请判断以下 {len(links)} 条审计链接，哪些属于该代币自己的审计资料：\n\n"
            f"{links_text}"
        )

        try:
            raw = self.chat(system_prompt, user_prompt, temperature=0.1, max_tokens=4096)
        except Exception as e:
            return [
                {"entry_id": l["entry_id"], "url": l["url"], "keep": True,
                 "reason": f"AI调用失败: {e}"}
                for l in links
            ]

        try:
            data = extract_json_from_llm_response(raw)
            results = data.get("results", [])
            if not isinstance(results, list):
                raise ValueError(f"results 不是列表，类型: {type(results).__name__}")
            result_map = {}
            for r in results:
                url = r.get("url", "")
                if url:
                    result_map[url] = {
                        "keep": bool(r.get("keep", True)),
                        "reason": str(r.get("reason", ""))[:200],
                    }
            return [
                {
                    "entry_id": l["entry_id"],
                    "url": l["url"],
                    "keep": result_map.get(l["url"], {}).get("keep", True),
                    "reason": result_map.get(l["url"], {}).get("reason", "未匹配到AI结果"),
                }
                for l in links
            ]
        except (json.JSONDecodeError, ValueError) as e:
            return [
                {"entry_id": l["entry_id"], "url": l["url"], "keep": True,
                 "reason": f"AI响应解析失败: {e}"}
                for l in links
            ]

    def judge_links_with_content(
        self,
        asset_symbol: str,
        asset_name: str,
        asset_description: str,
        items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        结合页面正文，二次判断不确定链接是否为噪声。

        Args:
            items: [{entry_id, url, title, text}]

        Returns:
            [{entry_id, url, noise: bool, reason: str}]
        """
        if not items:
            return []

        system_prompt = (
            "你是一个加密货币投研资料筛选专家。给定代币基础资料和一批链接的页面正文，"
            "判断每条链接是否为噪声。\n"
            "\n"
            "- noise=false（保留）：内容与该代币项目相关（官方文档、审计、生态、社区等）。\n"
            "- noise=true（删除）：内容与该代币无关、是其它项目文档、通用技术文档、"
            "学术论文、电商/企业网站等。\n"
            "\n"
            "⚠️ 原则：宁可保留，不可误删；无法确定则保留（noise=false）。\n"
            "\n"
            "只输出 JSON。JSON 格式：\n"
            '{"results": [{"url": "string", "noise": true/false, "reason": "简短理由"}]}'
        )

        parts = []
        for it in items:
            text = (it.get("text") or "").strip()[:3000]
            title = (it.get("title") or "").strip()[:300]
            parts.append(
                f'- url: {it["url"]}\n'
                f"  title: {title}\n"
                f"  正文摘要:\n    {text}"
            )
        items_text = "\n\n".join(parts)

        desc = (asset_description or "").strip()[:500]
        user_prompt = (
            f"代币基础资料：\n"
            f"- 符号: {asset_symbol}\n"
            f"- 名称: {asset_name}\n"
            f"- 简介: {desc or '（无）'}\n\n"
            f"请结合页面内容判断以下 {len(items)} 条链接是否为噪声：\n\n"
            f"{items_text}"
        )

        try:
            raw = self.chat(system_prompt, user_prompt, temperature=0.1, max_tokens=4096)
        except Exception as e:
            return [
                {"entry_id": it["entry_id"], "url": it["url"], "noise": False,
                 "reason": f"AI调用失败: {e}"}
                for it in items
            ]

        try:
            data = extract_json_from_llm_response(raw)
            results = data.get("results", [])
            if not isinstance(results, list):
                raise ValueError(f"results 不是列表，类型: {type(results).__name__}")
            result_map = {}
            for r in results:
                url = r.get("url", "")
                if url:
                    result_map[url] = {
                        "noise": bool(r.get("noise", False)),
                        "reason": str(r.get("reason", ""))[:200],
                    }
            return [
                {
                    "entry_id": it["entry_id"],
                    "url": it["url"],
                    "noise": result_map.get(it["url"], {}).get("noise", False),
                    "reason": result_map.get(it["url"], {}).get("reason", "未匹配到AI结果"),
                }
                for it in items
            ]
        except (json.JSONDecodeError, ValueError) as e:
            return [
                {"entry_id": it["entry_id"], "url": it["url"], "noise": False,
                 "reason": f"AI响应解析失败: {e}"}
                for it in items
            ]

    def batch_classify_content_topics(
        self,
        items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """批量对链接做「内容主题」多标签分类（结合页面正文/URL）。

        与 batch_check_crypto_relevance 只判断是否相关不同，本方法输出对齐
        taxonomy.CONTENT_TOPICS 的多标签，用于回填低置信度链接的内容主题。

        Args:
            items: [{entry_id, url, title, text}]，text 为页面正文摘要（可空，
                   为空时仅凭 URL + 标题判断，置信度应相应降低）。

        Returns:
            [{entry_id, url, content_topics: [str], confidence: float, reason: str}]
            调用失败/解析失败时 content_topics 为空、confidence 为 0，由调用方跳过。
        """
        if not items:
            return []

        # 主题定义：label 复用 taxonomy.CONTENT_TOPIC_LABELS，此处只补充简短判据
        topic_defs = [
            ("whitepaper", "白皮书/黄皮书/轻皮书，项目技术愿景"),
            ("docs", "技术文档/开发者文档/接口文档/使用说明"),
            ("audit", "安全审计报告/漏洞评估/代码审计"),
            ("deck", "项目路演 PPT / 融资 Deck"),
            ("tokenomics", "代币经济学/代币分配/释放/经济模型"),
            ("research", "行业或项目研究报告/深度分析"),
            ("announcement", "官方公告/新闻发布"),
            ("roadmap", "路线图/里程碑规划"),
            ("tge_ido", "TGE/IDO/IEO/公募/私募/代币发行"),
            ("lp_liquidity", "流动性池/LP/做市/AMM"),
            ("treasury_multisig", "国库/多签钱包/资金管理"),
            ("team_vc", "团队/创始人/投资人/融资"),
            ("dao_governance", "DAO/治理提案/投票"),
            ("bug_bounty", "漏洞赏金/安全披露"),
            ("exchange_listing", "交易所上线/上市/交易对"),
            ("competitor", "竞品对比/竞争分析"),
            ("major_event", "重大事件/迁移/升级/空投/主网上线"),
            ("third_party_rating", "第三方评级/数据平台(Dune/DefiLlama/Messari 等)"),
            ("onchain_abnormal", "链上异常/攻击/漏洞利用/黑客事件"),
            ("other", "无法归入以上任何一类"),
        ]
        topics_text = "\n".join(f'- "{k}": {v}' for k, v in topic_defs)

        system_prompt = (
            "你是加密货币投研资料分类专家。给定链接的 URL、标题与页面正文摘要，"
            "判断其内容主题，输出多标签分类结果。\n"
            "\n"
            "可选主题（可多选，至少选一个，最相关不超过 3 个）：\n"
            f"{topics_text}\n"
            "\n"
            "规则：\n"
            "- 优先依据页面正文内容判断，而不是仅凭 URL；正文为空时再参考 URL 与标题。\n"
            "- 优先选最贴切的主题；无法归入具体主题时选 other。\n"
            "- content_topics 只能使用上述 key，不要自造新词。\n"
            "- confidence 表示把握程度（0~1）：正文明确时给 0.8~0.95，"
            "仅凭 URL/标题推断时给 0.5~0.7。\n"
            "\n"
            "只输出 JSON，不要输出其他内容。JSON 格式：\n"
            '{"results": [{"entry_id": "string", "content_topics": ["..."], '
            '"confidence": 0.0, "reason": "简短理由"}]}'
        )

        parts = []
        for it in items:
            text = (it.get("text") or "").strip()[:2500]
            title = (it.get("title") or "").strip()[:200]
            parts.append(
                f'- entry_id: {it["entry_id"]}\n'
                f"  url: {it['url']}\n"
                f"  title: {title}\n"
                f"  正文摘要:\n    {text or '（无正文）'}"
            )
        items_text = "\n\n".join(parts)

        user_prompt = (
            f"请对以下 {len(items)} 个链接做内容主题多标签分类：\n\n{items_text}"
        )

        try:
            raw = self.chat(system_prompt, user_prompt, temperature=0.0, max_tokens=4096)
        except Exception as e:
            return [
                {"entry_id": str(it["entry_id"]), "url": it["url"],
                 "content_topics": [], "confidence": 0.0,
                 "reason": f"AI调用失败: {str(e)[:100]}"}
                for it in items
            ]

        try:
            data = extract_json_from_llm_response(raw)
            # LLM 可能返回 {"results": [...]}，也可能直接返回 [...] 数组
            if isinstance(data, dict):
                results = data.get("results", [])
            elif isinstance(data, list):
                results = data
            else:
                results = []
            if not isinstance(results, list):
                raise ValueError(f"results 不是列表，类型: {type(results).__name__}")

            def _key(v: Any) -> str:
                return str(v).strip()

            result_map: dict[str, dict] = {}
            for r in results:
                if not isinstance(r, dict):
                    continue
                rid = r.get("entry_id")
                rurl = r.get("url")
                if rid is None and not rurl:
                    continue
                topics = [
                    str(t) for t in (r.get("content_topics") or [])
                    if str(t) in CONTENT_TOPICS
                ]
                if not topics:
                    topics = ["other"]
                conf = max(0.0, min(1.0, float(r.get("confidence", 0.5))))
                entry = {
                    "content_topics": topics,
                    "confidence": conf,
                    "reason": str(r.get("reason", ""))[:200],
                }
                if rid is not None:
                    result_map[_key(rid)] = entry
                if rurl:
                    result_map.setdefault(_key(rurl), entry)

            def _lookup(it: dict[str, Any]) -> dict:
                return result_map.get(
                    _key(it["entry_id"]),
                    result_map.get(_key(it["url"]), {}),
                )

            return [
                {
                    "entry_id": str(it["entry_id"]),
                    "url": it["url"],
                    "content_topics": _lookup(it).get("content_topics", []),
                    "confidence": _lookup(it).get("confidence", 0.0),
                    "reason": _lookup(it).get("reason", "未匹配到AI结果"),
                }
                for it in items
            ]
        except (json.JSONDecodeError, ValueError) as e:
            self._last_diag["parse_error"] = str(e)
            self._last_diag["parse_raw_prefix"] = raw[:200]
            return [
                {"entry_id": str(it["entry_id"]), "url": it["url"],
                 "content_topics": [], "confidence": 0.0,
                 "reason": f"AI响应解析失败: {str(e)[:100]}"}
                for it in items
            ]
