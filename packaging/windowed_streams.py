"""Provide writable streams for libraries used by the windowed executable."""

import os
import sys


if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")
if sys.stdout is None:
    sys.stdout = sys.stderr
