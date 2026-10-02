import subprocess
import sys

from evals.runner.verifier import capture_patch, run_verifier


def test_verifier_records_zero_exit_as_pass(tmp_path):
    result = run_verifier(
        [sys.executable, "-c", "print('verified')"],
        workspace=tmp_path,
        timeout_seconds=5,
    )

    assert result.passed is True
    assert result.exit_code == 0
    assert result.stdout.strip() == "verified"
    assert result.stderr == ""
    assert result.duration_seconds >= 0


def test_verifier_records_nonzero_exit_as_failure(tmp_path):
    result = run_verifier(
        [sys.executable, "-c", "import sys; print('bad', file=sys.stderr); sys.exit(3)"],
        workspace=tmp_path,
        timeout_seconds=5,
    )

    assert result.passed is False
    assert result.exit_code == 3
    assert result.stderr.strip() == "bad"


def test_capture_patch_returns_binary_capable_git_diff(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "eval@example.invalid"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "CodeHarness Eval"],
        cwd=tmp_path,
        check=True,
    )
    target = tmp_path / "value.txt"
    target.write_text("before\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "baseline"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    target.write_text("after\n")

    patch, error = capture_patch(tmp_path)

    assert error is None
    assert "-before" in patch
    assert "+after" in patch
