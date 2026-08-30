"""Provider 注册与工厂：统一返回 base.py 的异步 LLMClient（providers 实现的接口）。

原来 client.py 定义了独立的同步 LLMClient 抽象，但没有任何 provider 实现它，
导致 main.py 通过 LLMClientFactory.create() 拿到的对象缺乏 review()/generate_structured()
真实实现，LLM 审查静默跳过。这里把工厂对齐到 providers 实际实现的异步接口。
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Sequence
from typing import TypeAlias, cast

from pydantic import BaseModel

from pr_guardian.llm.base import LLMClient, Message, StructuredOutputModel
from pr_guardian.models import Finding

JSONValue: TypeAlias = str | int | float | bool | None | dict[str, "JSONValue"] | list["JSONValue"]
JSONDict: TypeAlias = dict[str, JSONValue]


class UnsupportedLLMProviderError(ValueError):
    """保留该异常类型，是为了让调用方沿用原有的 provider 错误处理语义。"""


ProviderBuilder = Callable[[JSONDict], LLMClient]


class LLMClientFactory:
    """把 provider 注册集中管理，是为了让调用方不依赖具体实现模块。"""

    _providers: dict[str, ProviderBuilder] = {}

    @staticmethod
    def register(provider: str, builder: ProviderBuilder) -> None:
        normalized_provider = provider.strip().lower()
        if not normalized_provider:
            raise ValueError("provider 不能为空")
        LLMClientFactory._providers[normalized_provider] = builder

    @staticmethod
    def create(provider: str, config: JSONDict) -> LLMClient:
        """创建异步 LLMClient 实例（base.py 接口，providers 实现）。"""
        normalized_provider = provider.strip().lower()
        builder = LLMClientFactory._providers.get(normalized_provider)
        if builder is None:
            supported_providers = ", ".join(sorted(LLMClientFactory._providers)) or "<none>"
            raise UnsupportedLLMProviderError(
                f"不支持的 provider: {provider}，可用 provider: {supported_providers}"
            )
        return builder(config)

    @staticmethod
    def create_async(provider: str, config: JSONDict) -> LLMClient:
        """create 的别名，语义更明确：总是返回异步客户端。"""
        return LLMClientFactory.create(provider, config)


def _make_openai_compatible_builder(provider_name: str, default_base_url: str) -> ProviderBuilder:
    def builder(config: JSONDict) -> LLMClient:
        from pr_guardian.llm.providers.openai import OpenAICompatibleClient

        api_key = str(config.get("api_key", ""))
        base_url = str(config.get("base_url") or default_base_url)
        return OpenAICompatibleClient(
            api_key=api_key,
            base_url=base_url,
            use_response_format=bool(config.get("use_response_format", True)),
        )

    return builder


def register_default_providers() -> None:
    """注册内置 provider；幂等，避免重复注册。"""
    if "openai" in LLMClientFactory._providers:
        return
    LLMClientFactory.register("openai", _make_openai_compatible_builder("openai", "https://api.openai.com/v1"))
    LLMClientFactory.register("glm", _make_openai_compatible_builder("glm", "https://open.bigmodel.cn/api/paas/v4"))
    LLMClientFactory.register("kimi", _make_openai_compatible_builder("kimi", "https://api.moonshot.cn/v1"))
    LLMClientFactory.register("minimax", _make_openai_compatible_builder("minimax", "https://api.minimax.chat/v1"))
    LLMClientFactory.register("custom", _make_openai_compatible_builder("custom", ""))


class CachingLLMClient(LLMClient):
    """按请求语义缓存结果，是为了降低重复 review 的成本并稳定测试。"""

    def __init__(self, wrapped_client: LLMClient) -> None:
        self._wrapped_client: LLMClient = wrapped_client
        self._cache: dict[str, tuple[BaseModel | None, list[Finding]]] = {}
        self._cache_lock: threading.Lock = threading.Lock()

    @property
    def wrapped_client(self) -> LLMClient:
        return self._wrapped_client

    def _build_cache_key(
        self,
        model: str,
        messages: Sequence[Message],
        schema: type[BaseModel],
    ) -> str:
        payload = {
            "model": model,
            "messages": list(messages),
            "schema": schema.model_json_schema(),
        }
        serialized_payload = json.dumps(payload, sort_keys=True, ensure_ascii=True)
        return hashlib.sha256(serialized_payload.encode("utf-8")).hexdigest()

    async def generate_structured(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        schema: type[StructuredOutputModel],
    ) -> tuple[StructuredOutputModel | None, list[Finding]]:
        cache_key = self._build_cache_key(model, messages, schema)
        with self._cache_lock:
            cached_result = self._cache.get(cache_key)
        if cached_result is not None:
            cached_model, cached_findings = cached_result
            return cast(StructuredOutputModel | None, cached_model), list(cached_findings)

        generated_result = await self._wrapped_client.generate_structured(model=model, messages=messages, schema=schema)
        with self._cache_lock:
            self._cache[cache_key] = (generated_result[0], list(generated_result[1]))
        return generated_result

    def estimate_tokens(self, messages: Sequence[Message]) -> int:
        return self._wrapped_client.estimate_tokens(messages)

    async def aclose(self) -> None:
        await self._wrapped_client.aclose()


class BudgetLLMClient(LLMClient):
    """在客户端侧记录预算，是为了在 provider 计费接口不统一时仍能做统一约束。"""

    def __init__(
        self,
        wrapped_client: LLMClient,
        max_budget_usd: float,
        default_cost_per_1k_tokens: float = 0.0,
    ) -> None:
        if max_budget_usd < 0:
            raise ValueError("max_budget_usd 不能小于 0")
        if default_cost_per_1k_tokens < 0:
            raise ValueError("default_cost_per_1k_tokens 不能小于 0")
        self._wrapped_client: LLMClient = wrapped_client
        self._max_budget_usd: float = max_budget_usd
        self._default_cost_per_1k_tokens: float = default_cost_per_1k_tokens
        self._spent_usd: float = 0.0
        self._budget_lock: threading.Lock = threading.Lock()

    @property
    def wrapped_client(self) -> LLMClient:
        return self._wrapped_client

    @property
    def remaining_budget_usd(self) -> float:
        with self._budget_lock:
            return max(self._max_budget_usd - self._spent_usd, 0.0)

    def _estimate_cost_usd(self, messages: Sequence[Message]) -> float:
        total_tokens = self._wrapped_client.estimate_tokens(messages)
        if total_tokens > 0 and self._default_cost_per_1k_tokens > 0:
            return float(total_tokens) / 1000.0 * self._default_cost_per_1k_tokens
        return 0.0

    async def generate_structured(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        schema: type[StructuredOutputModel],
    ) -> tuple[StructuredOutputModel | None, list[Finding]]:
        with self._budget_lock:
            if self._spent_usd >= self._max_budget_usd:
                raise RuntimeError(f"LLM 预算已耗尽: {self._spent_usd:.6f}/{self._max_budget_usd:.6f} USD")

        generated_result = await self._wrapped_client.generate_structured(model=model, messages=messages, schema=schema)
        estimated_cost = self._estimate_cost_usd(messages)
        with self._budget_lock:
            next_spent_usd = self._spent_usd + estimated_cost
            if next_spent_usd > self._max_budget_usd:
                raise RuntimeError(f"LLM 预算超限: 预估 {next_spent_usd:.6f}/{self._max_budget_usd:.6f} USD")
            self._spent_usd = next_spent_usd
        return generated_result

    def estimate_tokens(self, messages: Sequence[Message]) -> int:
        return self._wrapped_client.estimate_tokens(messages)

    async def aclose(self) -> None:
        await self._wrapped_client.aclose()


def build_llm_client(config: JSONDict) -> LLMClient:
    """根据配置构建带缓存/预算包装的 LLMClient。

    config 支持: provider, api_key, base_url, model, budget_usd,
    cost_per_1k_tokens, max_context_tokens, use_response_format
    """
    register_default_providers()
    provider = str(config.get("provider", "openai"))
    client: LLMClient = LLMClientFactory.create(provider, config)

    max_budget_usd = float(config.get("budget_usd", 0.0))
    if max_budget_usd > 0:
        client = BudgetLLMClient(client, max_budget_usd, float(config.get("cost_per_1k_tokens", 0.0)))
    return CachingLLMClient(client)


__all__ = [
    "LLMClient",
    "LLMClientFactory",
    "CachingLLMClient",
    "BudgetLLMClient",
    "UnsupportedLLMProviderError",
    "register_default_providers",
    "build_llm_client",
    "JSONDict",
    "ProviderBuilder",
]
