"""gettext-based i18n for the web UI.

Translation catalogs are compiled .mo files under ``locales/<lang>/LC_MESSAGES/``.
English is not a catalog - it *is* the msgid text in every template, so an
untranslated or missing string simply falls back to the English source
instead of showing a blank string or a raw message key.

Extraction/update/compile commands are documented in README.md under
"Webbgränssnitt - Översättningar".
"""

from __future__ import annotations

import gettext as gettext_module
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.requests import Request

LOCALES_DIR = Path(__file__).parent / "locales"
DOMAIN = "messages"

# English is the fallback baked into every msgid, so it needs no catalog.
SUPPORTED_LANGUAGES = ("en", "sv")
DEFAULT_LANGUAGE = "en"
SESSION_KEY = "lang"

_translations_cache: dict[str, gettext_module.NullTranslations] = {}


def normalize_language(value: str | None) -> str:
    """Map an arbitrary value to a supported language code, defaulting to English."""
    if value in SUPPORTED_LANGUAGES:
        return value
    return DEFAULT_LANGUAGE


def _load_translations(lang: str) -> gettext_module.NullTranslations:
    if lang not in _translations_cache:
        try:
            _translations_cache[lang] = gettext_module.translation(
                DOMAIN, localedir=str(LOCALES_DIR), languages=[lang]
            )
        except FileNotFoundError:
            # No compiled catalog for this language (e.g. English, or a
            # catalog that hasn't been compiled yet) - fall back to the
            # msgid text unchanged rather than raising.
            _translations_cache[lang] = gettext_module.NullTranslations()
    return _translations_cache[lang]


def translate(lang: str, message: str) -> str:
    """Translate *message* into *lang*.

    Falls back to the original (English) string when the catalog has no
    entry for it, or when no catalog exists for the language at all.
    """
    return _load_translations(normalize_language(lang)).gettext(message)


def get_language(request: Request) -> str:
    """Resolve the active language for *request* from the session cookie."""
    session = getattr(request, "session", None)
    if session:
        value = session.get(SESSION_KEY)
        if value:
            return normalize_language(value)
    return DEFAULT_LANGUAGE


def set_language(request: Request, lang: str) -> str:
    """Persist *lang* (normalized) in the session and return the stored value."""
    normalized = normalize_language(lang)
    request.session[SESSION_KEY] = normalized
    return normalized
