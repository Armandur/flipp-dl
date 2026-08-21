"""Tests for the settings directory browser."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flipp_dl.web.app import create_app


@pytest.fixture()
def client(tmp_path: Path, monkeypatch) -> TestClient:
    monkeypatch.delenv("FLIPP_PASSWORD", raising=False)
    output_root = tmp_path / "output"
    output_root.mkdir()
    data_root = tmp_path / "data"
    data_root.mkdir()
    app = create_app(db_path=data_root / "flipp.db", output_root=output_root)
    return TestClient(app)


def test_directory_endpoint_navigates_inside_allowed_root(client: TestClient):
    child = client.app.state.output_root / "Comics"
    child.mkdir()

    root = client.get("/settings/directories", params={"root": "output"})
    assert root.status_code == 200
    assert root.json()["directories"] == [
        {"name": "Comics", "path": "Comics", "writable": True}
    ]

    below = client.get(
        "/settings/directories", params={"root": "output", "path": "Comics"}
    )
    assert below.status_code == 200
    assert below.json()["absolute_path"] == str(child.resolve())
    assert below.json()["parent"] == "."


def test_directory_endpoint_rejects_traversal_and_absolute_paths(
    client: TestClient, tmp_path: Path
):
    for path in ("..", "../outside", str(tmp_path.resolve())):
        response = client.get(
            "/settings/directories", params={"root": "output", "path": path}
        )
        assert response.status_code == 403


def test_directory_endpoint_rejects_symlink_outside_root(
    client: TestClient, tmp_path: Path
):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = client.app.state.output_root / "outside-link"
    link.symlink_to(outside, target_is_directory=True)

    direct = client.get(
        "/settings/directories",
        params={"root": "output", "path": "outside-link"},
    )
    assert direct.status_code == 403

    listing = client.get("/settings/directories", params={"root": "output"}).json()
    assert "outside-link" not in {
        directory["name"] for directory in listing["directories"]
    }


def test_directory_endpoint_lists_database_directory(client: TestClient):
    response = client.get("/settings/directories", params={"root": "data"})

    assert response.status_code == 200
    assert response.json()["absolute_path"] == str(
        client.app.state.db_path.parent.resolve()
    )


def test_settings_contains_directory_browser_button_and_dialog(client: TestClient):
    response = client.get("/settings")

    assert response.status_code == 200
    assert 'class="btn btn-primary-soft path-browser-open"' in response.text
    assert 'data-path-target="secondary_output_root"' in response.text
    assert 'id="path-browser"' in response.text
