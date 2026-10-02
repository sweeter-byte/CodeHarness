import subprocess
import sys
from pathlib import Path

from evals.runner.case import discover_cases


def test_all_smoke_fixtures_fail_their_verifier_at_baseline():
    suite_dir = Path(__file__).parents[1] / "cases" / "smoke"
    cases = discover_cases(suite_dir)

    assert len(cases) == 5
    for case in cases:
        command = list(case.verification.command)
        if command[0] in {"python", "python3"}:
            command[0] = sys.executable
        completed = subprocess.run(
            command,
            cwd=case.fixture_dir,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert completed.returncode != 0, (
            f"{case.case_id} unexpectedly passes at baseline:\n"
            f"{completed.stdout}\n{completed.stderr}"
        )
