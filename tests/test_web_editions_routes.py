"""Tests for the edition discovery/import background jobs (TASK-1444).

Both runs are queued from the settings page and drained by
:func:`flipp_dl.editions.run_editions_queue`. ``create_app`` does not start
a scheduler, so the routes are tested for what they queue and the drainer
is driven directly with a fake PageSuite client.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flipp_dl.db.models import JobStatus
from flipp_dl.db.repository import DownloadRepository
from flipp_dl.db.session import get_session
from flipp_dl.editions import (
    JOB_DISCOVER,
    JOB_IMPORT,
    reset_stuck_editions_jobs,
    run_editions_queue,
)
from flipp_dl.models import Issue, Publication
from flipp_dl.web.app import create_app


class _FakeMeta:
    def __init__(self, publication_guid, edition_name, iso_date):
        self.publication_guid = publication_guid
        self.edition_name = edition_name
        self.iso_date = iso_date


class _FakeClient:
    """Stands in for PageSuiteClient - no network, scripted answers."""

    def __init__(self, editions=None, metadata=None):
        self._editions = editions or {}
        self._metadata = metadata or {}
        self.edition_calls: list[str] = []

    def fetch_editions(self, code):
        self.edition_calls.append(code)
        return self._editions.get(code, [])

    def fetch_edition_metadata(self, eid):
        return self._metadata.get(eid)


@pytest.fixture()
def app(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("FLIPP_PASSWORD", raising=False)
    monkeypatch.setenv("FLIPP_COVER_CACHE", str(tmp_path / "covers"))
    output_root = tmp_path / "output"
    output_root.mkdir()
    app = create_app(db_path=tmp_path / "flipp.db", output_root=output_root)
    with get_session(app.state.session_factory) as session:
        repo = DownloadRepository(session)
        repo.upsert_publication(Publication(custom_code="KA", name="Kalle Anka"))
        repo.upsert_publication(Publication(custom_code="BI", name="Bilar"))
    return app


@pytest.fixture()
def client(app) -> TestClient:
    return TestClient(app)


def _csrf(client: TestClient) -> str:
    page = client.get("/settings")
    match = re.search(r'name="_csrf_token" value="([^"]+)"', page.text)
    assert match, "no CSRF token rendered on the settings page"
    return match.group(1)


def _jobs(app, job_type: str) -> list[dict]:
    """Job rows as plain dicts - the ORM objects detach with the session."""
    with get_session(app.state.session_factory) as session:
        return [
            {"id": j.id, "status": j.status, "payload": j.payload}
            for j in DownloadRepository(session).list_jobs(job_type=job_type)
        ]


# ---------------------------------------------------------------------------
# Routes - what they queue
# ---------------------------------------------------------------------------


def test_discover_route_queues_a_job(client: TestClient, app):
    token = _csrf(client)
    response = client.post("/settings/discover-editions", data={"_csrf_token": token})

    assert response.status_code == 200
    jobs = _jobs(app, JOB_DISCOVER)
    assert len(jobs) == 1
    assert jobs[0]["status"] == JobStatus.QUEUED
    # The partial must say what is happening, and keep polling for it.
    assert "editions-status" in response.text


def test_discover_route_requires_a_csrf_token(client: TestClient, app):
    _csrf(client)
    response = client.post("/settings/discover-editions")
    assert response.status_code == 400
    assert _jobs(app, JOB_DISCOVER) == []


def test_a_second_run_is_not_queued_while_one_is_in_flight(client: TestClient, app):
    token = _csrf(client)
    client.post("/settings/discover-editions", data={"_csrf_token": token})
    client.post("/settings/discover-editions", data={"_csrf_token": token})

    assert len(_jobs(app, JOB_DISCOVER)) == 1


def test_import_route_queues_the_uploaded_entries(client: TestClient, app):
    token = _csrf(client)
    payload = {"found_unlisted_issues": [{"issue_code": "shadow-1"}]}
    response = client.post(
        "/settings/import-editions",
        data={"_csrf_token": token},
        files={
            "editions": (
                "olistade.json",
                json.dumps(payload).encode("utf-8"),
                "application/json",
            )
        },
    )

    assert response.status_code == 200
    jobs = _jobs(app, JOB_IMPORT)
    assert len(jobs) == 1
    assert json.loads(jobs[0]["payload"])["input"]["entries"] == [
        {"issue_code": "shadow-1"}
    ]


def test_an_editions_file_with_no_editions_is_reported(client: TestClient, app):
    token = _csrf(client)
    response = client.post(
        "/settings/import-editions",
        data={"_csrf_token": token},
        files={"editions": ("tom.json", b"[]", "application/json")},
    )

    assert response.status_code == 200
    assert "no editions" in response.text
    assert _jobs(app, JOB_IMPORT) == []


def test_status_endpoint_reports_no_run_before_the_first_one(client: TestClient):
    response = client.get("/settings/editions-status")
    assert response.status_code == 200
    assert "No edition run has been started yet." in response.text


def test_status_stops_polling_once_the_job_is_finished(client: TestClient, app):
    token = _csrf(client)
    client.post("/settings/discover-editions", data={"_csrf_token": token})
    assert "hx-trigger" in client.get("/settings/editions-status").text

    job_id = _jobs(app, JOB_DISCOVER)[0]["id"]
    with get_session(app.state.session_factory) as session:
        repo = DownloadRepository(session)
        repo.merge_job_payload(
            job_id, {"result": {"new_editions": 3, "checked": 2, "failed": 0}}
        )
        repo.finish_job(job_id)

    finished = client.get("/settings/editions-status")
    assert "hx-trigger" not in finished.text
    assert "3" in finished.text


# ---------------------------------------------------------------------------
# The queue drainer
# ---------------------------------------------------------------------------


def test_drainer_runs_a_discovery_job_and_records_the_result(app):
    factory = app.state.session_factory
    with get_session(factory) as session:
        DownloadRepository(session).create_job(JOB_DISCOVER)

    client = _FakeClient(
        editions={
            "KA": [
                Issue(custom_code="ka01", issue_name="Nr 1", issue_date="2024-01-01")
            ],
            "BI": [],
        }
    )
    assert run_editions_queue(factory, client=client) == 1
    assert sorted(client.edition_calls) == ["BI", "KA"]

    with get_session(factory) as session:
        repo = DownloadRepository(session)
        job = repo.list_jobs(job_type=JOB_DISCOVER)[0]
        assert job.status == JobStatus.DONE
        payload = json.loads(job.payload)
        assert payload["result"]["new_editions"] == 1
        assert payload["result"]["checked"] == 2
        assert payload["progress"]["done"] == 2
        pub = repo.get_publication("KA")
        assert {i.custom_code for i in repo.list_issues(publication_id=pub.id)} == {
            "ka01"
        }


def test_drainer_imports_editions_and_counts_the_ones_it_cannot_use(app):
    factory = app.state.session_factory
    entries = [
        {"issue_code": "shadow-1"},
        {"issue_code": "dead-1"},
        {"issue_code": "orphan-1"},
    ]
    with get_session(factory) as session:
        DownloadRepository(session).create_job(
            JOB_IMPORT, {"input": {"entries": entries}}
        )

    client = _FakeClient(
        metadata={
            "shadow-1": _FakeMeta("KA", "Nr 9", "2026-02-01"),
            "dead-1": None,
            "orphan-1": _FakeMeta("okand", "Nr 1", "2026-01-01"),
        }
    )
    assert run_editions_queue(factory, client=client) == 1

    with get_session(factory) as session:
        repo = DownloadRepository(session)
        job = repo.list_jobs(job_type=JOB_IMPORT)[0]
        assert job.status == JobStatus.DONE
        result = json.loads(job.payload)["result"]
        assert result == {
            "imported": 1,
            "dead": 1,
            "no_publication": 1,
            "per_publication": [["Kalle Anka", 1]],
        }
        pub = repo.get_publication("KA")
        assert {i.custom_code for i in repo.list_issues(publication_id=pub.id)} == {
            "shadow-1"
        }


def test_a_failing_run_is_recorded_as_a_job_error(app):
    factory = app.state.session_factory
    with get_session(factory) as session:
        DownloadRepository(session).create_job(JOB_DISCOVER)

    class _Exploding:
        def fetch_editions(self, code):
            raise RuntimeError("PageSuite is down")

    assert run_editions_queue(factory, client=_Exploding()) == 1
    with get_session(factory) as session:
        job = DownloadRepository(session).list_jobs(job_type=JOB_DISCOVER)[0]
        assert job.status == JobStatus.ERROR
        assert "PageSuite is down" in job.error_message


def test_drainer_does_nothing_without_a_queued_job(app):
    assert run_editions_queue(app.state.session_factory, client=_FakeClient()) == 0


def test_a_run_interrupted_by_a_restart_is_requeued(app):
    factory = app.state.session_factory
    with get_session(factory) as session:
        repo = DownloadRepository(session)
        job = repo.create_job(JOB_DISCOVER)
        repo.start_job(job.id)

    assert reset_stuck_editions_jobs(factory) == 1
    with get_session(factory) as session:
        job = DownloadRepository(session).list_jobs(job_type=JOB_DISCOVER)[0]
        assert job.status == JobStatus.QUEUED
        assert job.started_at is None


def test_the_cli_and_the_web_share_one_editions_parser(tmp_path):
    """A bare list and the wrapped object must mean the same thing."""
    from flipp_dl.editions import parse_editions

    assert parse_editions(b'[{"issue_code": "a"}]') == [{"issue_code": "a"}]
    assert parse_editions(b'{"found_unlisted_issues": [{"issue_code": "a"}]}') == [
        {"issue_code": "a"}
    ]


def test_status_renders_progress_while_the_job_runs(client: TestClient, app):
    """The line a user actually watches - counts rendered, poll still on."""
    token = _csrf(client)
    client.post("/settings/discover-editions", data={"_csrf_token": token})
    job_id = _jobs(app, JOB_DISCOVER)[0]["id"]
    with get_session(app.state.session_factory) as session:
        repo = DownloadRepository(session)
        repo.start_job(job_id)
        repo.merge_job_payload(
            job_id, {"progress": {"done": 5, "total": 40, "found": 2}}
        )

    response = client.get("/settings/editions-status")

    assert "5 av 40 klara, 2 nya hittills." in _swedish(client)
    assert "5 of 40 done, 2 new so far." in response.text
    assert "hx-trigger" in response.text


def _swedish(client: TestClient) -> str:
    """The status partial rendered in Swedish."""
    client.get("/language/sv?next=/settings")
    text = client.get("/settings/editions-status").text
    client.get("/language/en?next=/settings")
    return text


def test_a_bad_upload_does_not_stop_the_poll_of_a_running_job(client: TestClient, app):
    token = _csrf(client)
    client.post("/settings/discover-editions", data={"_csrf_token": token})

    response = client.post(
        "/settings/import-editions",
        data={"_csrf_token": token},
        files={"editions": ("trasig.json", b"inte json", "application/json")},
    )

    assert response.status_code == 200
    assert "not valid JSON" in response.text
    # The queued discovery is still shown, and still polling.
    assert "hx-trigger" in response.text
    assert _jobs(app, JOB_IMPORT) == []


def test_progress_writes_are_capped_for_a_large_import(app):
    """A big upload must not be re-serialized once per entry."""
    factory = app.state.session_factory
    entries = [{"issue_code": f"e{i}"} for i in range(400)]
    with get_session(factory) as session:
        DownloadRepository(session).create_job(
            JOB_IMPORT, {"input": {"entries": entries}}
        )

    writes = 0
    original = DownloadRepository.merge_job_payload

    def counting(self, job_id, updates):
        nonlocal writes
        if "progress" in updates:
            writes += 1
        return original(self, job_id, updates)

    DownloadRepository.merge_job_payload = counting
    try:
        assert run_editions_queue(factory, client=_FakeClient()) == 1
    finally:
        DownloadRepository.merge_job_payload = original

    # 400 entries at one write per five would be 80; the ceiling holds it
    # to ~20 regardless of how large the file is.
    assert writes <= 21
