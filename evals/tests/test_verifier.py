import subprocess
import sys

from evals.runner.verifier import capture_patch, run_verifier


def _initialize_git_repository(tmp_path, files):
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
    for name, contents in files.items():
        path = tmp_path / name
        if isinstance(contents, bytes):
            path.write_bytes(contents)
        else:
            path.write_text(contents)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "baseline"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )


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


def test_capture_patch_includes_tracked_file_modification(tmp_path):
    _initialize_git_repository(tmp_path, {"value.txt": "before\n"})
    target = tmp_path / "value.txt"
    target.write_text("after\n")

    patch, error = capture_patch(tmp_path)

    assert error is None
    assert "-before" in patch
    assert "+after" in patch


def test_capture_patch_includes_new_untracked_text_file(tmp_path):
    _initialize_git_repository(tmp_path, {"tracked.txt": "baseline\n"})
    (tmp_path / "created.txt").write_text("new content\n")

    patch, error = capture_patch(tmp_path)

    assert error is None
    assert "diff --git a/created.txt b/created.txt" in patch
    assert "new file mode" in patch
    assert "+new content" in patch


def test_capture_patch_includes_tracked_file_deletion(tmp_path):
    _initialize_git_repository(tmp_path, {"deleted.txt": "remove me\n"})
    (tmp_path / "deleted.txt").unlink()

    patch, error = capture_patch(tmp_path)

    assert error is None
    assert "diff --git a/deleted.txt b/deleted.txt" in patch
    assert "deleted file mode" in patch
    assert "-remove me" in patch


def test_capture_patch_excludes_ignored_untracked_file(tmp_path):
    _initialize_git_repository(
        tmp_path,
        {".gitignore": "ignored.txt\n", "tracked.txt": "baseline\n"},
    )
    (tmp_path / "ignored.txt").write_text("do not include\n")

    patch, error = capture_patch(tmp_path)

    assert error is None
    assert patch == ""


def test_capture_patch_leaves_git_index_unchanged(tmp_path):
    _initialize_git_repository(tmp_path, {"tracked.txt": "baseline\n"})
    (tmp_path / "created.txt").write_text("new content\n")
    index_path = tmp_path / ".git" / "index"
    index_before = index_path.read_bytes()

    patch, error = capture_patch(tmp_path)

    assert error is None
    assert "created.txt" in patch
    assert index_path.read_bytes() == index_before
    cached_diff = subprocess.run(
        ["git", "diff", "--cached", "--exit-code"],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
    )
    assert cached_diff.returncode == 0
    staged_entry = subprocess.run(
        ["git", "ls-files", "--stage", "--", "created.txt"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert staged_entry.stdout == ""


def test_capture_patch_keeps_binary_diff_support(tmp_path):
    _initialize_git_repository(tmp_path, {"tracked.txt": "baseline\n"})
    (tmp_path / "created.bin").write_bytes(bytes(range(256)))

    patch, error = capture_patch(tmp_path)

    assert error is None
    assert "diff --git a/created.bin b/created.bin" in patch
    assert "GIT binary patch" in patch
