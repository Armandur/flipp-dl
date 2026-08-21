"""Tests for the PageSuite edition-list client (TASK-1439)."""

from __future__ import annotations

import pytest

from flipp_dl.pagesuite import (
    PageSuiteClient,
    PageSuiteError,
    _editions_from_payload,
    _normalise_date,
)


def test_normalise_date_converts_us_format_to_iso():
    assert _normalise_date("1/21/2026") == "2026-01-21"
    assert _normalise_date("12/5/2025") == "2025-12-05"


def test_normalise_date_keeps_unparseable_verbatim():
    assert _normalise_date("21 January 2026") == "21 January 2026"
    assert _normalise_date("") == ""


def test_editions_from_payload_reads_a_list():
    payload = {
        "editions": {
            "edition": [
                {"@editionguid": "eid-1", "@name": "Nr 1", "@date": "1/21/2026"},
                {"@editionguid": "eid-2", "@name": "Nr 2", "@date": "2/4/2026"},
            ]
        }
    }
    issues = _editions_from_payload(payload)
    assert [i.custom_code for i in issues] == ["eid-1", "eid-2"]
    assert issues[0].issue_name == "Nr 1"
    assert issues[0].issue_date == "2026-01-21"


def test_editions_from_payload_wraps_a_single_edition():
    # A publication with one edition is not wrapped in a list.
    payload = {"editions": {"edition": {"@editionguid": "solo", "@name": "Only"}}}
    issues = _editions_from_payload(payload)
    assert len(issues) == 1
    assert issues[0].custom_code == "solo"


def test_editions_from_payload_skips_entries_without_guid():
    payload = {"editions": {"edition": [{"@name": "no guid"}, {"@editionguid": "ok"}]}}
    issues = _editions_from_payload(payload)
    assert [i.custom_code for i in issues] == ["ok"]


def test_editions_from_payload_handles_empty_response():
    assert _editions_from_payload({}) == []
    assert _editions_from_payload({"editions": None}) == []
    assert _editions_from_payload({"editions": {}}) == []


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        if self._payload is _NON_JSON:
            raise ValueError("no json")
        return self._payload


_NON_JSON = object()


class _FakeSession:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        return self._response


def test_fetch_editions_returns_issues_and_sends_pubid():
    session = _FakeSession(
        _FakeResponse(
            {"editions": {"edition": [{"@editionguid": "e1", "@name": "Nr 1"}]}}
        )
    )
    client = PageSuiteClient(session=session)
    issues = client.fetch_editions("PUB-GUID", maxnumber=5000)
    assert [i.custom_code for i in issues] == ["e1"]
    # The pubid is passed as publicationguid - the field editionshtml5_json wants.
    _url, params, _headers = session.calls[0]
    assert params["publicationguid"] == "PUB-GUID"
    assert params["maxnumber"] == 5000


def test_fetch_editions_raises_on_http_error():
    session = _FakeSession(_FakeResponse({}, status=500))
    client = PageSuiteClient(session=session)
    with pytest.raises(PageSuiteError):
        client.fetch_editions("PUB")


def test_fetch_editions_raises_on_non_json():
    session = _FakeSession(_FakeResponse(_NON_JSON))
    client = PageSuiteClient(session=session)
    with pytest.raises(PageSuiteError):
        client.fetch_editions("PUB")
