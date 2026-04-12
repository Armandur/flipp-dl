"""flipp-dl – download publications from Flipp as PDF."""

from .api import FlippClient, FlippError
from .downloader import IssueDownloader
from .models import Category, Issue, Publication

__all__ = [
    "Category",
    "FlippClient",
    "FlippError",
    "Issue",
    "IssueDownloader",
    "Publication",
]

__version__ = "0.2.0"
