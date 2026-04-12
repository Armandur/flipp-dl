"""Minimal HTML sanitiser for third-party blurbs embedded in the UI.

Flipp's ``description`` field contains light HTML (``<p>``, ``<br>``,
``<strong>`` …). We render it via Jinja's ``| safe`` so the markup
survives, which means we must first strip anything that could execute
JavaScript. This is deliberately small and conservative – it is not a
full HTML sanitiser. If we ever need to handle untrusted user input
(rather than upstream-controlled content) switch to the ``bleach``
package.
"""

from __future__ import annotations

import re

_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)
_STYLE_RE = re.compile(r"<style\b[^>]*>.*?</style>", re.IGNORECASE | re.DOTALL)
_EVENT_ATTR_RE = re.compile(
    r"""\son[a-z]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""",
    re.IGNORECASE,
)
_JS_HREF_RE = re.compile(
    r"""(href|src)\s*=\s*(["'])\s*javascript:[^"']*\2""",
    re.IGNORECASE,
)
_IFRAME_RE = re.compile(r"<iframe\b[^>]*>.*?</iframe>", re.IGNORECASE | re.DOTALL)


def sanitize_html(html: str | None) -> str:
    """Return *html* with the most dangerous constructs stripped.

    Safe to pass to Jinja's ``| safe`` filter afterwards.
    """
    if not html:
        return ""
    html = _SCRIPT_RE.sub("", html)
    html = _STYLE_RE.sub("", html)
    html = _IFRAME_RE.sub("", html)
    html = _EVENT_ATTR_RE.sub("", html)
    html = _JS_HREF_RE.sub(r'\1="#"', html)
    return html
