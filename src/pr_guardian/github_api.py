from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DiffFile:
    filename: str
    status: str
    patch: str | None
    additions: int
    deletions: int


class GitHubAPIError(RuntimeError):
    """保留请求元信息，是为了让上层能区分网络、限流和返回体形状问题。"""

    def __init__(
        self,
        message: str,
        *,
        method: str,
        endpoint: str,
        status_code: int | None = None,
        attempt: int | None = None,
        response_body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.method = method
        self.endpoint = endpoint
        self.status_code = status_code
        self.attempt = attempt
        self.response_body = response_body


class GitHubAPIClient:
    """集中处理 GitHub API 的重试和形状校验，避免调用方散落网络边界逻辑。"""

    def __init__(
        self,
        token: str,
        repo: str,
        base_url: str = "https://api.github.com",
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.repo = repo
        self.base_url = base_url.rstrip("/")
        self.max_retries = 3
        self._default_headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        # 允许注入 client，是为了让测试复用假实现并隔离真实网络。
        if client is not None:
            self._client = client
        else:
            self._client = httpx.Client(base_url=self.base_url, headers=self._default_headers, timeout=30.0)

    def close(self) -> None:
        self._client.close()

    def get_pr_files(self, pr_number: int) -> list[DiffFile]:
        page = 1
        all_files: list[DiffFile] = []

        # GitHub files API 单页最多 100 条，必须聚合所有分页才能避免漏审。
        while True:
            response_files = self._request(
                "GET",
                f"/repos/{self.repo}/pulls/{pr_number}/files",
                params={"page": page, "per_page": 100},
            )
            if not isinstance(response_files, list):
                raise GitHubAPIError(
                    "GitHub API 返回格式错误: pulls files 不是列表",
                    method="GET",
                    endpoint=f"/repos/{self.repo}/pulls/{pr_number}/files",
                )
            if not response_files:
                break

            for file_item in response_files:
                all_files.append(
                    DiffFile(
                        filename=str(file_item.get("filename", "")),
                        status=str(file_item.get("status", "")),
                        patch=file_item.get("patch"),
                        additions=int(file_item.get("additions", 0)),
                        deletions=int(file_item.get("deletions", 0)),
                    )
                )
            page += 1

        return all_files

    def get_pr_details(self, pr_number: int) -> dict[str, Any]:
        pr_data = self._request("GET", f"/repos/{self.repo}/pulls/{pr_number}")
        if not isinstance(pr_data, dict):
            raise GitHubAPIError(
                "GitHub API 返回格式错误: pulls details 不是对象",
                method="GET",
                endpoint=f"/repos/{self.repo}/pulls/{pr_number}",
            )
        return {
            "title": pr_data.get("title"),
            "body": pr_data.get("body"),
            "base_sha": (pr_data.get("base") or {}).get("sha"),
            "head_sha": (pr_data.get("head") or {}).get("sha"),
            "raw": pr_data,
        }

    def create_check_run(
        self,
        name: str,
        head_sha: str,
        status: str,
        conclusion: str | None,
        output: dict[str, Any],
        annotations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": name,
            "head_sha": head_sha,
            "status": status,
            "output": output,
            "annotations": annotations,
        }
        if conclusion is not None:
            payload["conclusion"] = conclusion

        response_data = self._request(
            "POST",
            f"/repos/{self.repo}/check-runs",
            json=payload,
            expected_statuses={201},
        )
        return self._ensure_dict(response_data, "POST", f"/repos/{self.repo}/check-runs")

    def update_check_run(self, check_run_id: int, **kwargs: Any) -> dict[str, Any]:
        response_data = self._request(
            "PATCH",
            f"/repos/{self.repo}/check-runs/{check_run_id}",
            json=kwargs,
        )
        return self._ensure_dict(response_data, "PATCH", f"/repos/{self.repo}/check-runs/{check_run_id}")

    def create_review(
        self,
        pr_number: int,
        commit_id: str,
        body: str,
        comments: list[dict[str, Any]],
        event: str = "COMMENT",
    ) -> dict[str, Any]:
        payload = {
            "commit_id": commit_id,
            "body": body,
            "event": event,
            "comments": comments,
        }
        response_data = self._request(
            "POST",
            f"/repos/{self.repo}/pulls/{pr_number}/reviews",
            json=payload,
            expected_statuses={200, 201},
        )
        return self._ensure_dict(response_data, "POST", f"/repos/{self.repo}/pulls/{pr_number}/reviews")

    def create_pr_comment(self, pr_number: int, body: str) -> dict[str, Any]:
        response_data = self._request(
            "POST",
            f"/repos/{self.repo}/issues/{pr_number}/comments",
            json={"body": body},
            expected_statuses={201},
        )
        return self._ensure_dict(response_data, "POST", f"/repos/{self.repo}/issues/{pr_number}/comments")

    def list_pr_comments(self, pr_number: int) -> list[dict[str, Any]]:
        page = 1
        merged_comments: list[dict[str, Any]] = []
        endpoint = f"/repos/{self.repo}/issues/{pr_number}/comments"

        while True:
            response_data = self._request("GET", endpoint, params={"page": page, "per_page": 100})
            if not isinstance(response_data, list):
                raise GitHubAPIError(
                    "GitHub API 返回格式错误: issue comments 不是列表",
                    method="GET",
                    endpoint=endpoint,
                )
            if not response_data:
                break
            merged_comments.extend(comment for comment in response_data if isinstance(comment, dict))
            page += 1

        return merged_comments

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        expected_statuses: set[int] | None = None,
    ) -> Any:
        success_statuses = expected_statuses or {200}

        # 只对瞬时网络和限流错误重试，避免把确定性失败伪装成偶发抖动。
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self._client.request(
                    method,
                    endpoint,
                    params=params,
                    json=json,
                    headers=self._default_headers,
                )
            except httpx.HTTPError as exc:
                logger.exception(
                    "GitHub API 请求异常",
                    extra={
                        "method": method,
                        "endpoint": endpoint,
                        "attempt": attempt,
                        # 不记录 params 以避免敏感信息泄露
                    },
                )
                if attempt >= self.max_retries:
                    raise GitHubAPIError(
                        f"GitHub API 请求失败: method={method} endpoint={endpoint} attempt={attempt}",
                        method=method,
                        endpoint=endpoint,
                        attempt=attempt,
                    ) from exc
                time.sleep(float(2 ** (attempt - 1)))
                continue

            if response.status_code in success_statuses:
                return response.json()

            should_retry = self._should_retry(response)
            if should_retry and attempt < self.max_retries:
                delay_seconds = self._compute_backoff_seconds(response, attempt)
                logger.warning(
                    "GitHub API 返回可重试错误，准备退避重试",
                    extra={
                        "method": method,
                        "endpoint": endpoint,
                        "attempt": attempt,
                        "status_code": response.status_code,
                        "delay_seconds": delay_seconds,
                        # 不记录响应体以避免敏感信息泄露
                    },
                )
                time.sleep(delay_seconds)
                continue

            logger.error(
                "GitHub API 请求失败",
                extra={
                    "method": method,
                    "endpoint": endpoint,
                    "attempt": attempt,
                    "status_code": response.status_code,
                    # 不记录响应体以避免敏感信息泄露
                },
            )
            raise GitHubAPIError(
                "GitHub API 请求失败: "
                f"method={method} endpoint={endpoint} status={response.status_code} attempt={attempt}",
                method=method,
                endpoint=endpoint,
                status_code=response.status_code,
                attempt=attempt,
                response_body=response.text,
            )

        raise GitHubAPIError(
            f"GitHub API 请求失败: method={method} endpoint={endpoint}",
            method=method,
            endpoint=endpoint,
        )

    @staticmethod
    def _should_retry(response: httpx.Response) -> bool:
        if response.status_code in {500, 502, 503, 504}:
            return True

        if response.status_code == 403:
            # GitHub 会用 403 表示限流，所以不能把所有 403 都当成权限错误。
            retry_after = response.headers.get("Retry-After")
            rate_remaining = response.headers.get("X-RateLimit-Remaining")
            return retry_after is not None or rate_remaining == "0"

        return False

    @staticmethod
    def _compute_backoff_seconds(response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                retry_after_seconds = float(retry_after)
                if retry_after_seconds > 0:
                    # 优先尊重服务端退避建议，避免客户端指数退避和平台限流策略冲突。
                    return retry_after_seconds
            except ValueError:
                pass

        return float(2 ** (attempt - 1))

    @staticmethod
    def _ensure_dict(response_data: Any, method: str, endpoint: str) -> dict[str, Any]:
        if not isinstance(response_data, dict):
            raise GitHubAPIError(
                "GitHub API 返回格式错误: 期望对象",
                method=method,
                endpoint=endpoint,
            )
        return response_data
