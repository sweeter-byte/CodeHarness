"""Shared setup for codeharness.tools tests."""

import sys
from pathlib import Path

# Make the project root importable regardless of how pytest is invoked.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
