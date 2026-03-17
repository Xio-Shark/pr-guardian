from __future__ import annotations

import hashlib
import json
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TypeAlias

from pydantic import BaseModel
from typing_extensions import override

JSONValue: TypeAlias = str | int | float | bool | None | dict[str, "JSONValue"] | list["JSONValue"]
JSONDict: TypeAlias = dict[str, JSONValue]


class LLMClient(ABC):
    """统一结构化生成接口，是为了让不同 provider 能被同一审查流程替换。"""

    @abstractmethod
    def generate_structured(
        self,
        system_prompt: str,
        user_payload: JSONDict,
        schema: type[BaseModel],
        params: JSONDict,
    ) -> JSONDict:
        raise NotImplementedError


class UnsupportedLLMProviderError(ValueError):
    pass


class LLMClientFactory:
    """把 provider 注册集中管理，是为了让调用方不依赖具体实现模块。"""

    _providers: dict[str, Callable[[JSONDict], LLMClient]] = {}

    @staticmethod
    def register(provider: str, builder: Callable[[JSONDict], LLMClient]) -> None:
        normalized_provider = provider.strip().lower()
        if not normalized_provider:
            raise ValueError("provider 不能为空")
        LLMClientFactory._providers[normalized_provider] = builder

    @staticmethod
    def create(provider: str, config: JSONDict) -> LLMClient:
        normalized_provider = provider.strip().lower()
        builder = LLMClientFactory._providers.get(normalized_provider)
        if builder is None:
            supported_providers = ", ".join(sorted(LLMClientFactory._providers)) or "<none>"
            raise UnsupportedLLMProviderError(
                f"不支持的 provider: {provider}，可用 provider: {supported_providers}"
            )
        return builder(config)


class CachingLLMClient(LLMClient):
    """按请求语义缓存结果，是为了降低重复 review 的成本并稳定测试。"""

    def __init__(self, wrapped_client: LLMClient) -> None:
        self._wrapped_client: LLMClient = wrapped_client
        self._cache: dict[str, JSONDict] = {}
        self._cache_lock: threading.Lock = threading.Lock()

    @override
    def generate_structured(
        self,
        system_prompt: str,
        user_payload: JSONDict,
        schema: type[BaseModel],
        params: JSONDict,
    ) -> JSONDict:
        cache_key = self._build_cache_key(system_prompt, user_payload, schema, params)
        with self._cache_lock:
            cached_result = self._cache.get(cache_key)
        if cached_result is not None:
            return dict(cached_result)

        generated_result = self._wrapped_client.generate_structured(system_prompt, user_payload, schema, params)
        with self._cache_lock:
            self._cache[cache_key] = dict(generated_result)
        return generated_result

    @staticmethod
    def _build_cache_key(
        system_prompt: str,
        user_payload: JSONDict,
        schema: type[BaseModel],
        params: JSONDict,
    ) -> str:
        # 把 schema 和参数一起纳入 key，才能避免不同输出契约误命中缓存。
        payload = {
            "system_prompt": system_prompt,
            "user_payload": user_payload,
            "schema": schema.model_json_schema(),
            "params": params,
        }
        serialized_payload = json.dumps(payload, sort_keys=True, ensure_ascii=True)
        return hashlib.sha256(serialized_payload.encode("utf-8")).hexdigest()


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
    def remaining_budget_usd(self) -> float:
        with self._budget_lock:
            return max(self._max_budget_usd - self._spent_usd, 0.0)

    @override
    def generate_structured(
        self,
        system_prompt: str,
        user_payload: JSONDict,
        schema: type[BaseModel],
        params: JSONDict,
    ) -> JSONDict:
        with self._budget_lock:
            if self._spent_usd >= self._max_budget_usd:
                raise RuntimeError(f"LLM 预算已耗尽: {self._spent_usd:.6f}/{self._max_budget_usd:.6f} USD")

        generated_result = self._wrapped_client.generate_structured(system_prompt, user_payload, schema, params)
        estimated_cost = self._estimate_cost_usd(generated_result)
        with self._budget_lock:
            # 很多 provider 只有响应后才返回 usage，所以需要在真实成本落地后再二次校验。
            next_spent_usd = self._spent_usd + estimated_cost
            if next_spent_usd > self._max_budget_usd:
                raise RuntimeError(
                    f"LLM 预算超限: 预估 {next_spent_usd:.6f}/{self._max_budget_usd:.6f} USD"
                )
            self._spent_usd = next_spent_usd
        return generated_result

    def _estimate_cost_usd(self, generated_result: JSONDict) -> float:
        usage = generated_result.get("usage")
        if not isinstance(usage, dict):
            return 0.0

        reported_cost = usage.get("cost_usd")
        if isinstance(reported_cost, int | float):
            return max(float(reported_cost), 0.0)

        total_tokens = usage.get("total_tokens")
        if isinstance(total_tokens, int) and total_tokens > 0 and self._default_cost_per_1k_tokens > 0:
            return float(total_tokens) / 1000.0 * self._default_cost_per_1k_tokens
        return 0.0


__all__ = [
    "LLMClient",
    "LLMClientFactory",
    "CachingLLMClient",
    "BudgetLLMClient",
    "UnsupportedLLMProviderError",
]
