"""Paths shared by the source application and its portable Windows release."""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent
PORTABLE = getattr(sys, "frozen", False)
ASSETS = ROOT / ("assets" if PORTABLE else "packaging/assets")


def default_file_directory():
    if not PORTABLE:
        return ROOT
    documents = Path.home() / "Documents"
    return documents if documents.is_dir() else Path.home()
