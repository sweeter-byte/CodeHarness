"""Pytest collection boundaries for evaluation fixtures."""

# Smoke fixture tests intentionally fail before an agent edits their copied
# workspace. The harness preflight test executes them explicitly in subprocesses;
# the repository test suite must not collect them as its own tests.
collect_ignore_glob = ["cases/smoke/*/fixture/test_*.py"]
