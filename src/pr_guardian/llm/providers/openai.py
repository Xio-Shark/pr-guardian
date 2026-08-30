# pyright: reportMissingImports=false, reportMissingSuperCall=false
from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import httpx
from pydantic import ValidationError

from pr_guardian.llm.base import LLMClient, Message, StructuredOutputModel
from pr_guardian.models import Evidence, Finding, Severity

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BASE_DELAY_SECONDS = 0.5


class LLMProviderError(RuntimeError):
    def __init__(self, provider: str, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code


class OpenAICompatibleClient(LLMClient):
    provider_name = "openai-compatible"
    completion_path = "/chat/completions"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        use_response_format: bool,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_base_delay_seconds: float = DEFAULT_RETRY_BASE_DELAY_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep_func: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.max_retries = max_retries
        self.retry_base_delay_seconds = retry_base_delay_seconds
        self.use_response_format = use_response_format
        self.sleep_func = sleep_func
        self.client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    async def generate_structured(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        schema: type[StructuredOutputModel],
    ) -> tuple[StructuredOutputModel | None, list[Finding]]:
        request_payload: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
        }
        if self.use_response_format:
            request_payload["response_format"] = {"type": "json_object"}

        try:
            response_payload = await self._post_with_retry(payload=request_payload)
            parsed_json = self._extract_json_object(response_payload)
        except LLMProviderError as provider_error:
            return None, [self._provider_error_finding(str(provider_error), provider_error.status_code)]

        try:
            return schema.model_validate(parsed_json), []
        except ValidationError as schema_error:
            return None, [self._schema_error_finding(schema_error)]

    def estimate_tokens(self, messages: Sequence[Message]) -> int:
        total_chars = 0
        for message in messages:
            role = str(message.get("role", ""))
            content = message.get("content", "")
            total_chars += len(role) + len(str(content))
        return max(1, math.ceil(total_chars / 4))

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _post_with_retry(self, payload: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(self.max_retries):
            try:
                response = await self.client.post(self.completion_path, json=payload)
            except httpx.HTTPError as request_error:
                if attempt + 1 >= self.max_retries:
                    raise LLMProviderError(
                        provider=self.provider_name,
                        message=f"{self.provider_name} 请求失败: {request_error}",
                    ) from request_error
                await self.sleep_func(self.retry_base_delay_seconds * (2**attempt))
                continue

            if 500 <= response.status_code < 600:
                if attempt + 1 >= self.max_retries:
                    raise self._http_status_error(response)
                await self.sleep_func(self.retry_base_delay_seconds * (2**attempt))
                continue

            if response.is_error:
                raise self._http_status_error(response)

            try:
                payload_json: dict[str, Any] = response.json()
            except ValueError as invalid_json_error:
                raise LLMProviderError(
                    provider=self.provider_name,
                    message=f"{self.provider_name} 返回非 JSON 响应",
                    status_code=response.status_code,
                ) from invalid_json_error
            return payload_json

        raise LLMProviderError(provider=self.provider_name, message=f"{self.provider_name} 请求重试失败")

    def _http_status_error(self, response: httpx.Response) -> LLMProviderError:
        detail = response.text
        try:
            response_payload = response.json()
            error_obj = response_payload.get("error", {})
            if isinstance(error_obj, dict):
                detail = str(error_obj.get("message", detail))
        except ValueError:
            pass
        return LLMProviderError(
            provider=self.provider_name,
            status_code=response.status_code,
            message=f"{self.provider_name} HTTP {response.status_code}: {detail}",
        )

    def _extract_json_object(self, response_payload: dict[str, Any]) -> dict[str, Any]:
        choices = response_payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMProviderError(provider=self.provider_name, message=f"{self.provider_name} 响应缺少 choices")

        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            raise LLMProviderError(provider=self.provider_name, message=f"{self.provider_name} choices[0] 格式错误")

        message_obj = first_choice.get("message")
        if not isinstance(message_obj, dict):
            raise LLMProviderError(provider=self.provider_name, message=f"{self.provider_name} 响应缺少 message")

        content = message_obj.get("content")
        if isinstance(content, list):
            text_parts: list[str] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(str(block.get("text", "")))
            content = "".join(text_parts)

        if isinstance(content, dict):
            return content

        if not isinstance(content, str):
            raise LLMProviderError(provider=self.provider_name, message=f"{self.provider_name} content 格式错误")

        # 部分推理模型（如 DeepSeek-R1）会把 JSON 包在 ```json ... ``` 代码块里，先剥离再解析。
        stripped_content = self._strip_code_fence(content)

        try:
            parsed = json.loads(stripped_content)
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            pass

        match = re.search(r"\{.*\}", stripped_content, re.DOTALL)
        if match is None:
            raise LLMProviderError(provider=self.provider_name, message=f"{self.provider_name} content 不是 JSON")

        try:
            parsed_match = json.loads(match.group(0))
        except ValueError as parse_error:
            raise LLMProviderError(provider=self.provider_name, message=f"{self.provider_name} JSON 解析失败") from parse_error

        if not isinstance(parsed_match, dict):
            raise LLMProviderError(provider=self.provider_name, message=f"{self.provider_name} 结构化结果不是对象")
        return parsed_match

    @staticmethod
    def _strip_code_fence(content: str) -> str:
        stripped = content.strip()
        if not stripped.startswith("```"):
            return stripped
        # 去掉首行 ``` 或 ```json 围栏，以及末尾 ```。
        if stripped.endswith("```"):
            stripped = stripped[:-3]
        fence_line = stripped.find("\n")
        if fence_line != -1:
            stripped = stripped[fence_line + 1 :]
        return stripped.strip()

    def _schema_error_finding(self, schema_error: ValidationError) -> Finding:
        return Finding(
            id=f"llm/schema-validation#{self.provider_name}",
            rule_id="llm/schema-validation",
            title="LLM 输出不符合 Schema",
            severity=Severity.ERROR,
            message=f"{self.provider_name} 输出结构校验失败: {schema_error}",
            evidence=[
                Evidence(
                    file=f"llm/providers/{self.provider_name}.py",
                    line=1,
                    snippet=str(schema_error),
                )
            ],
            tags=["llm", "schema"],
            confidence=1.0,
        )

    def _provider_error_finding(self, message: str, status_code: int | None) -> Finding:
        status_suffix = "" if status_code is None else f" (status={status_code})"
        return Finding(
            id=f"llm/provider-error#{self.provider_name}",
            rule_id="llm/provider-error",
            title="LLM Provider 请求失败",
            severity=Severity.ERROR,
            message=f"{message}{status_suffix}",
            evidence=[
                Evidence(
                    file=f"llm/providers/{self.provider_name}.py",
                    line=1,
                    snippet=message,
                )
            ],
            tags=["llm", "provider"],
            confidence=1.0,
        )


class OpenAIClient(OpenAICompatibleClient):
    provider_name = "openai"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_base_delay_seconds: float = DEFAULT_RETRY_BASE_DELAY_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep_func: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            use_response_format=True,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            retry_base_delay_seconds=retry_base_delay_seconds,
            transport=transport,
            sleep_func=sleep_func,
        )


__all__ = ["LLMProviderError", "OpenAIClient", "OpenAICompatibleClient"]
