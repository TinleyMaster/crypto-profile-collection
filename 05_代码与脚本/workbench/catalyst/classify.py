"""
催化剂规则兜底分类器（快通道零 LLM）。

把 title + body_text 通过关键词正则匹配，产出 rule_event_type。
快通道 G1 分级依赖这个分类，慢通道 LLM 出来后再用 ai_event_type 覆写。

设计原则：
- 关键词表外置 catalyst_rules.yaml，不改代码
- 匹配优先级按列表顺序，命中即止
- 分类不准没关系——只是排序用的粗筛，最终决策有 G2/G5/G6 兜底

用法：
    from catalyst.classify import RuleEventClassifier
    clf = RuleEventClassifier(rules)
    event_type = clf.classify(title, body_text, related_pairs)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


@dataclass
class RuleEventClassifier:
    """基于关键词正则的规则分类器。"""

    # 规则列表，按优先级排序（从高到低）
    rules: list[dict]
    # token 提示正则（用于 require_token_hint 判断）
    token_hint_pattern: str = r"(?:[A-Z]{3,8}/?USDT|\$[A-Za-z0-9]{2,10}|\b[A-Z]{3,8}\b(?:/USDT|/USDC|/BTC|/ETH))"

    _compiled: bool = False
    _compiled_rules: list[tuple[str, re.Pattern, bool]] = None  # (event_type, pattern, require_token)
    _token_hint_re: re.Pattern = None

    def __post_init__(self):
        self._compile()

    def _compile(self):
        """预编译所有正则表达式。"""
        compiled = []
        for rule in self.rules:
            event_type = rule["event_type"]
            keywords = rule.get("keywords", [])
            require_token = rule.get("require_token_hint", False)

            if not keywords:
                # 兜底 other，无关键词
                compiled.append((event_type, None, require_token))
                continue

            # 构建正则：任意一个关键词命中即可
            # 关键词中的特殊字符转义
            escaped = [re.escape(kw) for kw in keywords]
            pattern = re.compile("|".join(escaped), re.IGNORECASE)
            compiled.append((event_type, pattern, require_token))

        self._compiled_rules = compiled
        self._token_hint_re = re.compile(self.token_hint_pattern)
        self._compiled = True

    def classify(self, title: str, body_text: str = "",
                 related_pairs: list[str] | None = None) -> str:
        """分类一条催化剂。

        Args:
            title: 标题
            body_text: 正文
            related_pairs: 关联交易对列表

        Returns:
            event_type 字符串（listing / delisting / burn / ... / other）
        """
        if not self._compiled:
            self._compile()

        text = (title or "") + " " + (body_text or "")
        has_pairs = bool(related_pairs)

        for event_type, pattern, require_token in self._compiled_rules:
            # other 兜底（无 pattern）
            if pattern is None:
                return event_type

            if pattern.search(text):
                # 需要 token 提示的事件，检查是否有 pair 或文本中有 token 迹象
                if require_token and not has_pairs:
                    if not self._token_hint_re.search(title or ""):
                        # 没有 token 提示，继续往下匹配
                        continue
                return event_type

        return "other"

    def classify_batch(self, items: Iterable[tuple[str, str, list[str] | None]]) -> list[str]:
        """批量分类。items = [(title, body_text, pairs), ...]"""
        return [self.classify(t, b, p) for t, b, p in items]


def load_rules_from_yaml(yaml_path: str) -> RuleEventClassifier:
    """从 YAML 配置文件加载分类规则。"""
    import yaml

    with open(yaml_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    return RuleEventClassifier(
        rules=config.get("rule_event_keywords", []),
        token_hint_pattern=config.get("token_hint_pattern", RuleEventClassifier.token_hint_pattern),
    )
