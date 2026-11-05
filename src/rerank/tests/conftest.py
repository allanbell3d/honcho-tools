"""Shared set-up for the offline tests. They need a Honcho checkout on PYTHONPATH."""

import importlib.util
import sys
from unittest.mock import MagicMock

# Honcho's trace logger imports POSIX locking, missing on Windows. These tests never
# write trace files.
if "fcntl" not in sys.modules and importlib.util.find_spec("fcntl") is None:
    sys.modules.setdefault("fcntl", MagicMock())
