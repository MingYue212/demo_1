"""GitHub REST 客户端的查询编码、认证、重试和输入校验测试。"""

import json
from urllib.error import HTTPError

import pytest

from radar.github import GitHubClient, GitHubError


def test_search_encodes_query_and_tracks_rate_limit():
    """搜索参数应正确编码，并记录响应中的剩余配额。"""
    requests = []

    def transport(request, timeout):
        """记录请求，模拟一个空搜索结果。"""
        requests.append((request, timeout))
        return json.dumps({"items": []}).encode(), {"X-RateLimit-Remaining": "42"}

    client = GitHubClient("secret", transport=transport)
    response = client.search_repositories("topic:llm archived:false", per_page=25)

    assert response.data == {"items": []}
    assert "topic%3Allm+archived%3Afalse" in requests[0][0].full_url
    assert requests[0][0].get_header("Authorization") == "Bearer secret"
    assert client.rate_limit_remaining == 42


def test_retryable_http_error_is_retried():
    """503 和 Retry-After 应触发一次可控重试。"""
    attempts = 0
    sleeps = []

    def transport(request, timeout):
        """第一次失败，第二次返回成功响应。"""
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise HTTPError(request.full_url, 503, "unavailable", {"Retry-After": "0"}, None)
        return b'{"items": []}', {}

    GitHubClient(transport=transport, sleep=sleeps.append).search_repositories("topic:llm")

    assert attempts == 2
    assert sleeps == [0.0]


def test_non_retryable_http_error_is_safe():
    """401 不应被重试，而应转换成 GitHubError。"""
    def transport(request, timeout):
        """模拟无效凭据响应。"""
        raise HTTPError(request.full_url, 401, "bad credentials", {}, None)

    with pytest.raises(GitHubError, match="HTTP 401"):
        GitHubClient(transport=transport).get_repository("owner/repo")


def test_repository_name_is_validated():
    """仓库路径必须严格使用 owner/name 格式。"""
    with pytest.raises(ValueError, match="owner/name"):
        GitHubClient().get_repository("invalid")
