from datetime import date

import httpx
import pytest

from movie_alert.config import CgvSettings, HttpSettings
from movie_alert.providers.base import ProviderError
from movie_alert.providers.cgv import CgvProvider
from movie_alert.providers.cgv_relay import CgvRelayProvider


@pytest.mark.parametrize("provider_type", [CgvProvider, CgvRelayProvider])
@pytest.mark.parametrize("timeout_seconds", [0.5, 7.0])
def test_providers_preserve_request_timeouts_and_public_headers(provider_type, timeout_seconds):
    requests = []

    def handler(request):
        requests.append(request)
        if provider_type is CgvProvider:
            return httpx.Response(200, json={"statusCode": 0, "data": []})
        # Also exercise the relay's movie-list request through the shared settings.
        if request.url.path.endswith("/timetable"):
            return httpx.Response(502)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "playDate": "20261024",
                    "filters": {"theaterCode": "0056"},
                    "movies": [],
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = provider_type(
            client, CgvSettings("A420", "08"), HttpSettings(timeout_seconds=timeout_seconds)
        )
        assert provider.fetch_showtimes("30001367", "0056", date(2026, 10, 24)) == []
    assert len(requests) == (1 if provider_type is CgvProvider else 2)
    for request in requests:
        assert request.extensions["timeout"] == {
            "connect": min(5.0, timeout_seconds),
            "read": timeout_seconds,
            "write": timeout_seconds,
            "pool": timeout_seconds,
        }
        assert request.headers["accept"] == "application/json"
        assert request.headers["user-agent"] == "movie-alert/0.1"
        assert "cookie" not in request.headers
        assert "authorization" not in request.headers


@pytest.mark.parametrize("provider_type", [CgvProvider, CgvRelayProvider])
def test_providers_reject_redirects_without_following_them(provider_type):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://other.invalid/"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = provider_type(client, CgvSettings("A420", "08"), HttpSettings())
        with pytest.raises(ProviderError, match="HTTP 302"):
            provider.fetch_showtimes("30001367", "0056", date(2026, 10, 24))
    assert len(requests) == 1
