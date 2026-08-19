"""Tests for KomgaClient (TASK-1326, TASK-1327)."""

from __future__ import annotations

import pytest
import requests

from flipp_dl.komga import (
    ISSUE_METADATA_FIELDS,
    PUBLICATION_METADATA_FIELDS,
    KomgaClient,
    KomgaError,
    cover_push_enabled,
    filter_pushed_fields,
    html_to_plain_text,
    parse_issue_number,
)


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

    def request(
        self,
        method,
        url,
        *,
        headers=None,
        auth=None,
        timeout=None,
        params=None,
        json=None,
        files=None,
    ):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "auth": auth,
                "timeout": timeout,
                "params": params,
                "json": json,
                "files": files,
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


# ---------------------------------------------------------------------------
# find_series_by_name (TASK-1327)
# ---------------------------------------------------------------------------


def test_find_series_by_name_matches_exact_name():
    series = [
        {"id": 1, "name": "Kalle Anka", "metadata": {"title": ""}},
        {"id": 2, "name": "Kalle Anka & Co", "metadata": {"title": ""}},
    ]
    session = FakeSession(FakeResponse(200, {"content": series}))
    client = KomgaClient("http://localhost:25600", api_key="k", session=session)

    result = client.find_series_by_name("lib-1", "Kalle Anka & Co")

    assert result == series[1]
    call = session.calls[0]
    assert call["method"] == "get"
    assert call["url"] == "http://localhost:25600/api/v1/series"
    assert call["params"] == {"search": "Kalle Anka & Co", "library_id": "lib-1"}


def test_find_series_by_name_matches_metadata_title():
    series = [{"id": 3, "name": "kalle-anka", "metadata": {"title": "Kalle Anka & Co"}}]
    session = FakeSession(FakeResponse(200, {"content": series}))
    client = KomgaClient("http://localhost:25600", api_key="k", session=session)

    result = client.find_series_by_name("lib-1", "Kalle Anka & Co")

    assert result == series[0]


def test_find_series_by_name_returns_none_without_exact_match():
    series = [{"id": 1, "name": "Kalle Anka Extra", "metadata": {}}]
    session = FakeSession(FakeResponse(200, {"content": series}))
    client = KomgaClient("http://localhost:25600", api_key="k", session=session)

    assert client.find_series_by_name("lib-1", "Kalle Anka") is None


def test_find_series_by_name_raises_on_http_failure():
    session = FakeSession(FakeResponse(500))
    client = KomgaClient("http://localhost:25600", api_key="k", session=session)

    with pytest.raises(KomgaError):
        client.find_series_by_name("lib-1", "Kalle Anka")


# ---------------------------------------------------------------------------
# patch_series_metadata / patch_book_metadata
# ---------------------------------------------------------------------------


def test_patch_series_metadata_sends_patch_with_fields():
    session = FakeSession(FakeResponse(204))
    client = KomgaClient("http://localhost:25600", api_key="k", session=session)

    client.patch_series_metadata(42, title="Kalle Anka & Co", publisher="Egmont")

    call = session.calls[0]
    assert call["method"] == "patch"
    assert call["url"] == "http://localhost:25600/api/v1/series/42/metadata"
    assert call["json"] == {"title": "Kalle Anka & Co", "publisher": "Egmont"}


def test_patch_series_metadata_is_noop_with_no_fields():
    session = FakeSession()
    client = KomgaClient("http://localhost:25600", api_key="k", session=session)

    client.patch_series_metadata(42)

    assert session.calls == []


def test_patch_series_metadata_raises_on_http_failure():
    session = FakeSession(FakeResponse(500))
    client = KomgaClient("http://localhost:25600", api_key="k", session=session)

    with pytest.raises(KomgaError):
        client.patch_series_metadata(42, title="x")


def test_patch_book_metadata_sends_patch_with_fields():
    session = FakeSession(FakeResponse(204))
    client = KomgaClient("http://localhost:25600", api_key="k", session=session)

    client.patch_book_metadata(7, title="Nr 12", number="12")

    call = session.calls[0]
    assert call["method"] == "patch"
    assert call["url"] == "http://localhost:25600/api/v1/books/7/metadata"
    assert call["json"] == {"title": "Nr 12", "number": "12"}


# ---------------------------------------------------------------------------
# list_series_books / find_book_by_stems
# ---------------------------------------------------------------------------


def test_find_book_by_stems_matches_either_form():
    books = [
        {"id": 1, "name": "Kalle Anka - 2024-01-01 - Nr 1 (ABCDEFGH)"},
        {"id": 2, "name": "Kalle Anka - 2024-01-08 - Nr 2"},
    ]
    session = FakeSession(FakeResponse(200, {"content": books}))
    client = KomgaClient("http://localhost:25600", api_key="k", session=session)

    plain = client.find_book_by_stems(
        9, {"Kalle Anka - 2024-01-08 - Nr 2", "Kalle Anka - 2024-01-08 - Nr 2 (X)"}
    )
    disambiguated = client.find_book_by_stems(
        9,
        {
            "Kalle Anka - 2024-01-01 - Nr 1",
            "Kalle Anka - 2024-01-01 - Nr 1 (ABCDEFGH)",
        },
    )

    assert plain == books[1]
    assert disambiguated == books[0]


def test_find_book_by_stems_returns_none_when_not_found():
    session = FakeSession(FakeResponse(200, {"content": []}))
    client = KomgaClient("http://localhost:25600", api_key="k", session=session)

    assert client.find_book_by_stems(9, {"anything"}) is None


def test_list_series_books_raises_on_http_failure():
    session = FakeSession(FakeResponse(500))
    client = KomgaClient("http://localhost:25600", api_key="k", session=session)

    with pytest.raises(KomgaError):
        client.list_series_books(9)


# ---------------------------------------------------------------------------
# get_book_read_progress (TASK-1328)
# ---------------------------------------------------------------------------


def test_get_book_read_progress_completed():
    session = FakeSession(
        FakeResponse(200, {"id": 7, "readProgress": {"page": 24, "completed": True}})
    )
    client = KomgaClient("http://localhost:25600", api_key="k", session=session)

    progress = client.get_book_read_progress(7)

    call = session.calls[0]
    assert call["method"] == "get"
    assert call["url"] == "http://localhost:25600/api/v1/books/7"
    assert progress == {"read": True, "page": 24, "completed": True}


def test_get_book_read_progress_never_opened():
    session = FakeSession(FakeResponse(200, {"id": 7, "readProgress": None}))
    client = KomgaClient("http://localhost:25600", api_key="k", session=session)

    progress = client.get_book_read_progress(7)

    assert progress == {"read": False, "page": 0, "completed": False}


def test_get_book_read_progress_in_progress_not_completed():
    session = FakeSession(
        FakeResponse(200, {"id": 7, "readProgress": {"page": 5, "completed": False}})
    )
    client = KomgaClient("http://localhost:25600", api_key="k", session=session)

    progress = client.get_book_read_progress(7)

    assert progress == {"read": False, "page": 5, "completed": False}


def test_get_book_read_progress_raises_on_http_failure():
    session = FakeSession(FakeResponse(500))
    client = KomgaClient("http://localhost:25600", api_key="k", session=session)

    with pytest.raises(KomgaError):
        client.get_book_read_progress(7)


# ---------------------------------------------------------------------------
# upload_series_thumbnail
# ---------------------------------------------------------------------------


def test_upload_series_thumbnail_posts_multipart_file():
    session = FakeSession(FakeResponse(204))
    client = KomgaClient("http://localhost:25600", api_key="k", session=session)

    client.upload_series_thumbnail(42, b"\x89PNG...", "cover.jpg")

    call = session.calls[0]
    assert call["method"] == "post"
    assert call["url"] == "http://localhost:25600/api/v1/series/42/thumbnails"
    assert call["params"] == {"selected": "true"}
    assert call["files"]["file"][0] == "cover.jpg"
    assert call["files"]["file"][1] == b"\x89PNG..."
    assert call["files"]["file"][2] == "image/jpeg"


def test_upload_series_thumbnail_raises_on_http_failure():
    session = FakeSession(FakeResponse(500))
    client = KomgaClient("http://localhost:25600", api_key="k", session=session)

    with pytest.raises(KomgaError):
        client.upload_series_thumbnail(42, b"data", "cover.jpg")


# ---------------------------------------------------------------------------
# filter_pushed_fields / per-field feature flags
# ---------------------------------------------------------------------------


def test_filter_pushed_fields_drops_none_values():
    fields = {"title": "X", "summary": None}
    assert filter_pushed_fields(fields, PUBLICATION_METADATA_FIELDS) == {"title": "X"}


def test_filter_pushed_fields_drops_disabled_field(monkeypatch):
    monkeypatch.setenv("KOMGA_PUSH_SUMMARY", "false")
    fields = {"title": "X", "summary": "blurb"}

    result = filter_pushed_fields(fields, PUBLICATION_METADATA_FIELDS)

    assert result == {"title": "X"}


def test_filter_pushed_fields_keeps_field_by_default(monkeypatch):
    monkeypatch.delenv("KOMGA_PUSH_SUMMARY", raising=False)
    fields = {"summary": "blurb"}

    assert filter_pushed_fields(fields, PUBLICATION_METADATA_FIELDS) == {
        "summary": "blurb"
    }


def test_filter_pushed_fields_issue_number_flag(monkeypatch):
    monkeypatch.setenv("KOMGA_PUSH_NUMBER", "0")
    fields = {"title": "Nr 1", "number": "1", "numberSort": 1.0}

    result = filter_pushed_fields(fields, ISSUE_METADATA_FIELDS)

    assert result == {"title": "Nr 1"}


def test_cover_push_enabled_defaults_true(monkeypatch):
    monkeypatch.delenv("KOMGA_PUSH_COVER", raising=False)
    assert cover_push_enabled() is True


def test_cover_push_enabled_false_when_disabled(monkeypatch):
    monkeypatch.setenv("KOMGA_PUSH_COVER", "false")
    assert cover_push_enabled() is False


# ---------------------------------------------------------------------------
# parse_issue_number
# ---------------------------------------------------------------------------


def test_parse_issue_number_extracts_digits():
    assert parse_issue_number("Nr 12") == ("12", 12.0)


def test_parse_issue_number_without_nr_prefix():
    assert parse_issue_number("12") == ("12", 12.0)


def test_parse_issue_number_returns_none_without_digits():
    assert parse_issue_number("Julnummer") == (None, None)


def test_parse_issue_number_handles_none():
    assert parse_issue_number(None) == (None, None)


# ---------------------------------------------------------------------------
# html_to_plain_text
# ---------------------------------------------------------------------------


def test_html_to_plain_text_strips_tags_and_keeps_line_breaks():
    html_body = "<p>Kalle Anka &amp; Co<br>är kul.</p><p>Nästa nummer snart.</p>"

    result = html_to_plain_text(html_body)

    assert "<p>" not in result
    assert "<br>" not in result
    assert "Kalle Anka & Co" in result
    assert "är kul." in result
    assert "Nästa nummer snart." in result


def test_html_to_plain_text_strips_script_tags():
    html_body = "<p>Hej</p><script>alert(1)</script>"

    result = html_to_plain_text(html_body)

    assert "alert" not in result
    assert "Hej" in result


def test_html_to_plain_text_handles_none_and_empty():
    assert html_to_plain_text(None) == ""
    assert html_to_plain_text("") == ""


# ---------------------------------------------------------------------------
# run_komga_read_status_sync (TASK-1328) - the scheduled job that drains
# issues with a cached ``komga_book_id`` and writes back the read status.
# Placed here rather than test_scheduler.py per this task's file scope.
# ---------------------------------------------------------------------------


@pytest.fixture()
def _read_sync_repo():
    from flipp_dl.db.repository import DownloadRepository
    from flipp_dl.db.session import make_session_factory

    factory = make_session_factory(":memory:")
    session = factory()
    repo = DownloadRepository(session)
    yield factory, repo
    session.close()


def _read_sync_publication():
    from flipp_dl.models import Issue, Publication

    return Publication(
        custom_code="KA",
        name="Kalle Anka & Co",
        issues=[
            Issue(custom_code="KA-01", issue_name="Nr 1", issue_date="2024-01-01"),
            Issue(custom_code="KA-02", issue_name="Nr 2", issue_date="2024-01-15"),
        ],
    )


def test_run_komga_read_status_sync_is_noop_when_disabled(_read_sync_repo):
    import flipp_dl.scheduler as scheduler_mod

    factory, repo = _read_sync_repo
    repo.upsert_publication(_read_sync_publication())
    repo.session.commit()

    processed = scheduler_mod.run_komga_read_status_sync(factory)

    assert processed == 0


def test_run_komga_read_status_sync_updates_mapped_issues(monkeypatch, _read_sync_repo):
    import flipp_dl.scheduler as scheduler_mod

    factory, repo = _read_sync_repo
    repo.sync_publications([_read_sync_publication()])
    repo.session.commit()
    issue_id = repo.get_publication("KA").issues[0].id
    repo.set_komga_book_id(issue_id, 77)
    repo.session.commit()
    repo.set_setting("komga_enabled", "true")
    repo.set_setting("komga_url", "http://localhost:25600")
    repo.session.commit()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def get_book_read_progress(self, book_id):
            assert book_id == 77
            return {"read": True, "page": 12, "completed": True}

    monkeypatch.setattr(scheduler_mod, "KomgaClient", FakeClient)

    processed = scheduler_mod.run_komga_read_status_sync(factory)

    assert processed == 1
    updated = repo.get_issue(issue_id)
    assert updated.komga_read is True
    assert updated.komga_read_page == 12
    assert updated.komga_read_synced_at is not None


def test_run_komga_read_status_sync_skips_issue_on_komga_error(
    monkeypatch, _read_sync_repo
):
    import flipp_dl.scheduler as scheduler_mod
    from flipp_dl.komga import KomgaError

    factory, repo = _read_sync_repo
    repo.sync_publications([_read_sync_publication()])
    repo.session.commit()
    issue_id = repo.get_publication("KA").issues[0].id
    repo.set_komga_book_id(issue_id, 77)
    repo.session.commit()
    repo.set_setting("komga_enabled", "true")
    repo.set_setting("komga_url", "http://localhost:25600")
    repo.session.commit()

    class FailingClient:
        def __init__(self, *args, **kwargs):
            pass

        def get_book_read_progress(self, book_id):
            raise KomgaError("Komga is down")

    monkeypatch.setattr(scheduler_mod, "KomgaClient", FailingClient)

    processed = scheduler_mod.run_komga_read_status_sync(factory)

    assert processed == 0
    assert repo.get_issue(issue_id).komga_read is None


def test_run_komga_read_status_sync_ignores_unmapped_issues(
    monkeypatch, _read_sync_repo
):
    import flipp_dl.scheduler as scheduler_mod

    factory, repo = _read_sync_repo
    repo.upsert_publication(_read_sync_publication())
    repo.session.commit()
    repo.set_setting("komga_enabled", "true")
    repo.set_setting("komga_url", "http://localhost:25600")
    repo.session.commit()

    class ExplodingClient:
        def __init__(self, *args, **kwargs):
            pass

        def get_book_read_progress(self, book_id):
            raise AssertionError("should never be called - no book mapped")

    monkeypatch.setattr(scheduler_mod, "KomgaClient", ExplodingClient)

    processed = scheduler_mod.run_komga_read_status_sync(factory)

    assert processed == 0
