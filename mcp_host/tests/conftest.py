"""Shared fixtures for mcp_host tests."""

import sys
from pathlib import Path

import pytest

# Make the project root importable regardless of how pytest is invoked.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mcp_host.runtime import SyncMCPRuntime


@pytest.fixture
def runtime():
    """A private runtime per test; always shut down afterwards."""
    rt = SyncMCPRuntime()
    yield rt
    rt.shutdown()
