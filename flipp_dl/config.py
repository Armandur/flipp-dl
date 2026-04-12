"""Runtime configuration helpers (token loading, default paths)."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_OUTPUT_DIR = Path("Output")
TOKEN_ENV_VAR = "FLIPP_TOKEN"
TOKEN_FILENAME = "token"


def load_token(project_root: Path | None = None) -> str:
    """Load the Flipp token from the environment or a local file.

    Lookup order:
    1. ``FLIPP_TOKEN`` environment variable.
    2. A ``token`` file located at *project_root* (defaults to CWD).
    """
    env_token = os.environ.get(TOKEN_ENV_VAR)
    if env_token:
        return env_token.strip()

    root = Path(project_root) if project_root else Path.cwd()
    token_file = root / TOKEN_FILENAME
    if token_file.is_file():
        return token_file.read_text(encoding="utf-8").strip()

    return ""


def default_output_path() -> Path:
    return Path.cwd() / DEFAULT_OUTPUT_DIR
