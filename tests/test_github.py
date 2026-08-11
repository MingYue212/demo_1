import json
from urllib.error import HTTPError

import pytest

from radar.github import GitHubClient, GitHubError


def test_search_encodes_query_and_tracks_rate_limit():
    requests = []

    def transport(request, timeout):
        requests.append((request, timeout))
        return json.dumps({"items": []}).encode(), {"X-RateLimit-Remaining": "42"}

    client = GitHubClient("secret", transport=transport)
    response = client.search_repositories("topic:llm archived:false", per_page=25)

    assert response.data == {"items": []}
    assert "topic%3Allm+archived%3Afalse" in requests[0][0].full_url
    assert requests[0][0].get_header("Authorization") == "Bearer secret"
    assert client.rate_limit_remaining == 42


def test_retryable_http_error_is_retried():
    attempts = 0
    sleeps = []

    def transport(request, timeout):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise HTTPError(request.full_url, 503, "unavailable", {"Retry-After": "0"}, None)
        return b'{"items": []}', {}

    GitHubClient(transport=transport, sleep=sleeps.append).search_repositories("topic:llm")

    assert attempts == 2
    assert sleeps == [0.0]


def test_non_retryable_http_error_is_safe():
    def transport(request, timeout):
        raise HTTPError(request.full_url, 401, "bad credentials", {}, None)

    with pytest.raises(GitHubError, match="HTTP 401"):
        GitHubClient(transport=transport).get_repository("owner/repo")


def test_repository_name_is_validated():
    with pytest.raises(ValueError, match="owner/name"):
        GitHubClient().get_repository("invalid")
