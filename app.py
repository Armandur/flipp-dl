"""Backwards compatible entry point.

The real implementation lives in the :mod:`flipp_dl` package. This file
remains so that ``python app.py`` keeps working for existing users.
Prefer ``python -m flipp_dl`` going forward.
"""

import sys

from flipp_dl.cli import main

if __name__ == "__main__":
    sys.exit(main())
