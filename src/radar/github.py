"""Small GitHub REST client with explicit rate-limit and retry handling."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class GitHubError(RuntimeError):
    """Raised when GitHub cannot satisfy a request."""


@dataclass(frozen=True)
class Response:
    data: dict[str, Any]
    headers: dict[str, str]


Transport = Callable[[Request, float], tuple[bytes, dict[str, str]]]


def _urlopen_transport(request: Request, timeout: float) -> tuple[bytes, dict[str, str]]:
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed API host
        return response.read(), dict(response.headers.items())


class GitHubClient:
    """A dependency-free client for the endpoints needed by the collector."""

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
        self.token = token
        self.timeout = timeout
        self.retries = retries
        self.transport = transport
        self.sleep = sleep
        self.rate_limit_remaining: int | None = None

    def search_repositories(self, query: str, *, per_page: int = 100) -> Response:
        params = urlencode({"q": query, "sort": "updated", "order": "desc", "per_page": per_page})
        return self._get(f"/search/repositories?{params}")

    def get_repository(self, full_name: str) -> Response:
        if full_name.count("/") != 1:
            raise ValueError("repository must use owner/name format")
        return self._get(f"/repos/{full_name}")

    def _get(self, path: str) -> Response:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "ai-tech-radar/0.1",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(f"{self.api_url}{path}", headers=headers)

        for attempt in range(self.retries + 1):
            try:
                payload, response_headers = self.transport(request, self.timeout)
                normalized = {key.lower(): value for key, value in response_headers.items()}
                remaining = normalized.get("x-ratelimit-remaining")
                self.rate_limit_remaining = int(remaining) if remaining is not None else None
                decoded = json.loads(payload)
                if not isinstance(decoded, dict):
                    raise GitHubError("GitHub returned a non-object response")
                return Response(decoded, normalized)
            except HTTPError as error:
                retryable = error.code in {429, 500, 502, 503, 504}
                if retryable and attempt < self.retries:
                    retry_after = float(error.headers.get("Retry-After", 2**attempt))
                    self.sleep(retry_after)
                    continue
                raise GitHubError(f"GitHub request failed with HTTP {error.code}") from error
            except (URLError, TimeoutError) as error:
                if attempt < self.retries:
                    self.sleep(2**attempt)
                    continue
                raise GitHubError("GitHub request failed after retries") from error
            except json.JSONDecodeError as error:
                raise GitHubError("GitHub returned invalid JSON") from error

        raise AssertionError("retry loop exited unexpectedly")
