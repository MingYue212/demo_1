"""面向采集器的轻量 GitHub REST 客户端。

客户端只使用 Python 标准库，显式处理认证头、API 版本、请求超时、剩余
配额和临时错误重试，便于在没有第三方运行时依赖的环境中执行采集任务。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class GitHubError(RuntimeError):
    """GitHub 无法满足请求时抛出的统一异常。"""


@dataclass(frozen=True)
class Response:
    """对 GitHub JSON 响应和规范化响应头的最小封装。"""

    data: dict[str, Any]
    headers: dict[str, str]


Transport = Callable[[Request, float], tuple[bytes, dict[str, str]]]


def _urlopen_transport(request: Request, timeout: float) -> tuple[bytes, dict[str, str]]:
    """使用标准库发起请求，单独抽出以便测试时注入 fake transport。"""
    # 请求 URL 由客户端固定拼接到 api.github.com，不接受外部主机输入。
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - API 主机由客户端固定
        return response.read(), dict(response.headers.items())


class GitHubClient:
    """采集器所需 GitHub endpoint 的无第三方依赖客户端。"""

    api_url = "https://api.github.com"

    def __init__(
        self,
        token: str | None = None,
        *,
        timeout: float = 20,
        retries: int = 2,
        transport: Transport = _urlopen_transport,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """保存请求策略，并允许测试替换网络传输和等待函数。"""
        self.token = token
        self.timeout = timeout
        self.retries = retries
        self.transport = transport
        self.sleep = sleep
        self.rate_limit_remaining: int | None = None

    def search_repositories(self, query: str, *, per_page: int = 100) -> Response:
        """按 GitHub 搜索表达式查找仓库，并按最近更新时间排序。"""
        # urlencode 保证 topic:、空格等查询字符不会破坏 URL。
        params = urlencode({"q": query, "sort": "updated", "order": "desc", "per_page": per_page})
        return self._get(f"/search/repositories?{params}")

    def get_repository(self, full_name: str) -> Response:
        """读取一个 owner/name 格式的仓库详情。"""
        if full_name.count("/") != 1:
            raise ValueError("repository must use owner/name format")
        return self._get(f"/repos/{full_name}")

    def _get(self, path: str) -> Response:
        """执行 GET 请求，记录配额并对可恢复错误进行有限次数重试。"""
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "ai-tech-radar/0.1",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            # Bearer token 只在用户配置 token 时加入，未认证试跑仍然可用。
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(f"{self.api_url}{path}", headers=headers)

        for attempt in range(self.retries + 1):
            try:
                payload, response_headers = self.transport(request, self.timeout)
                # GitHub header 名称不区分大小写，统一转小写方便后续读取。
                normalized = {key.lower(): value for key, value in response_headers.items()}
                remaining = normalized.get("x-ratelimit-remaining")
                self.rate_limit_remaining = int(remaining) if remaining is not None else None
                decoded = json.loads(payload)
                if not isinstance(decoded, dict):
                    raise GitHubError("GitHub returned a non-object response")
                return Response(decoded, normalized)
            except HTTPError as error:
                # 429 和 5xx 通常是暂时性故障，其他状态码直接交给调用方处理。
                retryable = error.code in {429, 500, 502, 503, 504}
                if retryable and attempt < self.retries:
                    retry_after = float(error.headers.get("Retry-After", 2**attempt))
                    self.sleep(retry_after)
                    continue
                raise GitHubError(f"GitHub request failed with HTTP {error.code}") from error
            except (URLError, TimeoutError) as error:
                # 网络错误采用指数退避；达到上限后不再无限等待。
                if attempt < self.retries:
                    self.sleep(2**attempt)
                    continue
                raise GitHubError("GitHub request failed after retries") from error
            except json.JSONDecodeError as error:
                raise GitHubError("GitHub returned invalid JSON") from error

        raise AssertionError("retry loop exited unexpectedly")
