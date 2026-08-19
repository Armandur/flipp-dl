"""Tests for KomgaClient (TASK-1326)."""

from __future__ import annotations

import pytest
import requests

from flipp_dl.komga import KomgaClient, KomgaError


class FakeResponse:
    def __init__(self, status_code: int = 200, json_data=None):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else []

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error", response=self)

    def json(self):
        return self._json_data


class FakeSession:
    """Records every call and returns a canned response."""

    def __init__(self, response: FakeResponse | None = None, raise_exc=None):
        self.calls: list[dict] = []
        self.response = response or FakeResponse()
        self.raise_exc = raise_exc

    def request(self, method, url, *, headers=None, auth=None, timeout=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "auth": auth,
                "timeout": timeout,
            }
        )
        if self.raise_exc:
            raise self.raise_exc
        return self.response


# ---------------------------------------------------------------------------
# list_libraries
# ---------------------------------------------------------------------------


def test_list_libraries_returns_parsed_json():
    libraries = [{"id": "lib-1", "name": "Comics"}, {"id": "lib-2", "name": "Manga"}]
    session = FakeSession(FakeResponse(200, libraries))
    client = KomgaClient("http://localhost:25600", api_key="secret", session=session)

    result = client.list_libraries()

    assert result == libraries
    assert session.calls[0]["method"] == "get"
    assert session.calls[0]["url"] == "http://localhost:25600/api/v1/libraries"


def test_list_libraries_raises_komga_error_on_http_failure():
    session = FakeSession(FakeResponse(401))
    client = KomgaClient("http://localhost:25600", api_key="wrong", session=session)

    with pytest.raises(KomgaError):
        client.list_libraries()


def test_list_libraries_raises_komga_error_on_connection_failure():
    session = FakeSession(raise_exc=requests.ConnectionError("boom"))
    client = KomgaClient("http://localhost:25600", api_key="key", session=session)

    with pytest.raises(KomgaError):
        client.list_libraries()


# ---------------------------------------------------------------------------
# scan_library
# ---------------------------------------------------------------------------


def test_scan_library_posts_to_scan_endpoint():
    session = FakeSession(FakeResponse(202, {}))
    client = KomgaClient("http://localhost:25600", api_key="secret", session=session)

    client.scan_library("lib-1")

    assert session.calls[0]["method"] == "post"
    assert session.calls[0]["url"] == (
        "http://localhost:25600/api/v1/libraries/lib-1/scan"
    )


def test_scan_library_raises_komga_error_on_http_failure():
    session = FakeSession(FakeResponse(500))
    client = KomgaClient("http://localhost:25600", api_key="secret", session=session)

    with pytest.raises(KomgaError):
        client.scan_library("lib-1")


def test_scan_library_strips_trailing_slash_from_url():
    session = FakeSession(FakeResponse(202, {}))
    client = KomgaClient("http://localhost:25600/", api_key="secret", session=session)

    client.scan_library("lib-1")

    assert session.calls[0]["url"] == (
        "http://localhost:25600/api/v1/libraries/lib-1/scan"
    )


# ---------------------------------------------------------------------------
# Auth variants
# ---------------------------------------------------------------------------


def test_uses_x_api_key_header_when_api_key_configured():
    session = FakeSession()
    client = KomgaClient(
        "http://localhost:25600",
        username="alice",
        password="pw",
        api_key="secret",
        session=session,
    )

    client.list_libraries()

    call = session.calls[0]
    assert call["headers"] == {"X-API-Key": "secret"}
    # API key wins over basic auth when both are configured.
    assert call["auth"] is None


def test_uses_http_basic_auth_when_no_api_key_configured():
    session = FakeSession()
    client = KomgaClient(
        "http://localhost:25600", username="alice", password="pw", session=session
    )

    client.list_libraries()

    call = session.calls[0]
    assert call["headers"] == {}
    assert call["auth"] == ("alice", "pw")


def test_no_auth_headers_when_nothing_configured():
    session = FakeSession()
    client = KomgaClient("http://localhost:25600", session=session)

    client.list_libraries()

    call = session.calls[0]
    assert call["headers"] == {}
    assert call["auth"] is None
