"""Tests for the notification channels (TASK-1293)."""

from __future__ import annotations

import pytest
import requests

from flipp_dl.notify import NotifyError, NtfyChannel, WebhookChannel, send_all


class _FakeResponse:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")


class _FakeSession:
    """Records every POST it receives; returns a canned response."""

    def __init__(
        self, response: _FakeResponse | None = None, exc: Exception | None = None
    ):
        self.calls: list[dict] = []
        self._response = response or _FakeResponse()
        self._exc = exc

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self._exc is not None:
            raise self._exc
        return self._response


# ---------------------------------------------------------------------------
# NtfyChannel
# ---------------------------------------------------------------------------


def test_ntfy_channel_posts_json_body_with_topic_title_message():
    session = _FakeSession()
    channel = NtfyChannel("https://ntfy.example.com", "flipp-dl", session=session)

    channel.send("Ny utgåva", "Kalle Anka & Co - Nr 1")

    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"] == "https://ntfy.example.com/"
    assert call["json"] == {
        "topic": "flipp-dl",
        "title": "Ny utgåva",
        "message": "Kalle Anka & Co - Nr 1",
    }
    assert "Authorization" not in call["headers"]


def test_ntfy_channel_sends_bearer_token_when_configured():
    session = _FakeSession()
    channel = NtfyChannel(
        "https://ntfy.example.com", "flipp-dl", "secret-token", session=session
    )

    channel.send("title", "message")

    assert session.calls[0]["headers"]["Authorization"] == "Bearer secret-token"


def test_ntfy_channel_strips_trailing_slash_from_url():
    session = _FakeSession()
    channel = NtfyChannel("https://ntfy.example.com/", "flipp-dl", session=session)

    channel.send("title", "message")

    assert session.calls[0]["url"] == "https://ntfy.example.com/"


def test_ntfy_channel_raises_notify_error_without_topic():
    channel = NtfyChannel("https://ntfy.example.com", "", session=_FakeSession())
    with pytest.raises(NotifyError):
        channel.send("title", "message")


def test_ntfy_channel_raises_notify_error_on_request_exception():
    session = _FakeSession(exc=requests.ConnectionError("boom"))
    channel = NtfyChannel("https://ntfy.example.com", "flipp-dl", session=session)
    with pytest.raises(NotifyError):
        channel.send("title", "message")


def test_ntfy_channel_raises_notify_error_on_http_error_status():
    session = _FakeSession(response=_FakeResponse(500))
    channel = NtfyChannel("https://ntfy.example.com", "flipp-dl", session=session)
    with pytest.raises(NotifyError):
        channel.send("title", "message")


# ---------------------------------------------------------------------------
# WebhookChannel
# ---------------------------------------------------------------------------


def test_webhook_channel_posts_title_and_message_as_json():
    session = _FakeSession()
    channel = WebhookChannel("https://hooks.example.com/flipp", session=session)

    channel.send("Ny utgåva", "Kalle Anka & Co - Nr 1")

    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"] == "https://hooks.example.com/flipp"
    assert call["json"] == {"title": "Ny utgåva", "message": "Kalle Anka & Co - Nr 1"}


def test_webhook_channel_raises_notify_error_without_url():
    channel = WebhookChannel("", session=_FakeSession())
    with pytest.raises(NotifyError):
        channel.send("title", "message")


def test_webhook_channel_raises_notify_error_on_request_exception():
    session = _FakeSession(exc=requests.ConnectionError("boom"))
    channel = WebhookChannel("https://hooks.example.com/flipp", session=session)
    with pytest.raises(NotifyError):
        channel.send("title", "message")


def test_webhook_channel_raises_notify_error_on_http_error_status():
    session = _FakeSession(response=_FakeResponse(404))
    channel = WebhookChannel("https://hooks.example.com/flipp", session=session)
    with pytest.raises(NotifyError):
        channel.send("title", "message")


# ---------------------------------------------------------------------------
# send_all
# ---------------------------------------------------------------------------


class _RecordingChannel:
    name = "recording"

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send(self, title, message):
        self.sent.append((title, message))


class _ExplodingChannel:
    name = "exploding"

    def send(self, title, message):
        raise NotifyError("channel is down")


class _BuggyChannel:
    """Raises something other than NotifyError - still must not propagate."""

    name = "buggy"

    def send(self, title, message):
        raise RuntimeError("unexpected bug")


def test_send_all_delivers_to_every_channel():
    a, b = _RecordingChannel(), _RecordingChannel()
    send_all([a, b], "title", "message")
    assert a.sent == [("title", "message")]
    assert b.sent == [("title", "message")]


def test_send_all_never_raises_when_a_channel_fails():
    good = _RecordingChannel()
    channels = [_ExplodingChannel(), good]

    send_all(channels, "title", "message")  # must not raise

    assert good.sent == [("title", "message")]


def test_send_all_never_raises_on_an_unexpected_exception():
    good = _RecordingChannel()
    channels = [_BuggyChannel(), good]

    send_all(channels, "title", "message")  # must not raise

    assert good.sent == [("title", "message")]


def test_send_all_with_no_channels_is_a_noop():
    send_all([], "title", "message")
