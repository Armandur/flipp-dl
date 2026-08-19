"""Notification channels for flipp-dl (TASK-1293).

Two channels are supported: :class:`NtfyChannel` (push via a self-hosted or
public ntfy server) and :class:`WebhookChannel` (POST a JSON payload to any
URL). Both implement the same :class:`NotificationChannel` interface, so the
call site (``run_download_queue`` in :mod:`flipp_dl.scheduler`) never needs
to know which channel(s) are configured - it just calls :func:`send_all`
with whatever :func:`flipp_dl.scheduler.build_notify_channels` returned.
Adding a third channel later means adding one more subclass here; nothing
at the call site changes.

Follows the same session/retry-light style as :mod:`flipp_dl.komga`, but
notifications are best-effort: a failure here must never surface as a
download or job failure (see :func:`send_all`).
"""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10


class NotifyError(Exception):
    """Raised when a channel fails to deliver a notification."""


class NotificationChannel:
    """Common interface every notification channel implements.

    ``name`` is used only for logging - it identifies which channel
    failed when :func:`send_all` swallows an error.
    """

    name = "channel"

    def send(self, title: str, message: str) -> None:
        """Deliver *title*/*message*. Raise :class:`NotifyError` on failure."""
        raise NotImplementedError


class NtfyChannel(NotificationChannel):
    """Publishes to an ntfy topic via its JSON publish endpoint.

    Uses the JSON body form (``POST {url}/`` with a ``topic`` field)
    rather than the header form (``POST {url}/{topic}`` with a ``Title``
    header) - ntfy's title/message headers must be latin-1-safe, and
    publication/issue names are Swedish text, so the JSON body avoids an
    encoding failure on every non-ASCII title.
    """

    name = "ntfy"

    def __init__(
        self,
        url: str,
        topic: str,
        token: str = "",
        *,
        session: requests.Session | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self.url = url.rstrip("/")
        self.topic = topic
        self.token = token
        self.session = session or requests.Session()
        self.timeout = timeout

    def send(self, title: str, message: str) -> None:
        if not self.topic:
            raise NotifyError("ntfy: no topic configured")
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        payload = {"topic": self.topic, "title": title, "message": message}
        try:
            response = self.session.post(
                f"{self.url}/", json=payload, headers=headers, timeout=self.timeout
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise NotifyError(
                f"ntfy: failed to publish to topic {self.topic!r}: {exc}"
            ) from exc


class WebhookChannel(NotificationChannel):
    """POSTs a small JSON payload (``title``, ``message``) to any URL."""

    name = "webhook"

    def __init__(
        self,
        url: str,
        *,
        session: requests.Session | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self.url = url
        self.session = session or requests.Session()
        self.timeout = timeout

    def send(self, title: str, message: str) -> None:
        if not self.url:
            raise NotifyError("webhook: no URL configured")
        payload = {"title": title, "message": message}
        try:
            response = self.session.post(self.url, json=payload, timeout=self.timeout)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise NotifyError(
                f"webhook: failed to POST to configured URL: {exc}"
            ) from exc


def send_all(channels: list[NotificationChannel], title: str, message: str) -> None:
    """Send *title*/*message* on every channel in *channels*.

    A channel failure (network error, bad status, misconfiguration) is
    only logged, never raised - the download or job that triggered the
    notification has already succeeded and must stay that way regardless
    of whether anyone got told about it (same rule as Komga sync
    failures in :mod:`flipp_dl.scheduler`).
    """
    for channel in channels:
        try:
            channel.send(title, message)
        except NotifyError as exc:
            logger.error("Notification via %s failed: %s", channel.name, exc)
        except Exception as exc:  # noqa: BLE001 - a channel bug must not break a job
            logger.error(
                "Notification via %s failed unexpectedly: %s", channel.name, exc
            )
