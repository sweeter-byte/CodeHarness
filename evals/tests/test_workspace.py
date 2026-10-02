import subprocess
from pathlib import Path

from evals.runner.case import EvalCase, Verification
from evals.runner.workspace import WorkspaceManager


def _case(tmp_path, case_id="sample"):
    fixture = tmp_path / f"{case_id}-fixture"
    fixture.mkdir()
    (fixture / "value.txt").write_text("baseline", encoding="utf-8")
    return EvalCase(
        case_id=case_id,
        category="bugfix",
        description="fixture",
        task="change it",
        fixture_dir=fixture,
        timeout_seconds=10,
        verification=Verification(("python", "-c", "pass")),
    )


def test_prepare_copies_fixture_without_mutating_source_and_initializes_git(
    tmp_path,
):
    repo_root = tmp_path / "source-repo"
    repo_root.mkdir()
    manager = WorkspaceManager(
        run_id="run-one",
        work_root=tmp_path / "work-root",
        repository_root=repo_root,
    )
    case = _case(tmp_path)

    prepared = manager.prepare(case)
    (prepared.workspace / "value.txt").write_text("changed", encoding="utf-8")

    assert (case.fixture_dir / "value.txt").read_text() == "baseline"
    assert prepared.agent_home == prepared.case_root / "agent-home"
    assert not prepared.workspace.is_relative_to(repo_root)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=prepared.workspace,
        capture_output=True,
        text=True,
        check=True,
    )
    assert head.stdout.strip()
    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=prepared.workspace,
        capture_output=True,
        text=True,
        check=True,
    )
    assert status.stdout.strip() == "M value.txt"


def test_two_cases_receive_distinct_workspaces(tmp_path):
    manager = WorkspaceManager(
        run_id="run-one",
        work_root=tmp_path / "work-root",
        repository_root=tmp_path / "repo",
    )

    first = manager.prepare(_case(tmp_path, "first"))
    second = manager.prepare(_case(tmp_path, "second"))

    assert first.workspace != second.workspace
    assert first.agent_home != second.agent_home


def test_manager_rejects_work_root_inside_codeharness_repository(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    try:
        WorkspaceManager(
            run_id="run-one",
            work_root=repo_root / "eval-work",
            repository_root=repo_root,
        )
    except ValueError as exc:
        assert "outside" in str(exc)
    else:
        raise AssertionError("repository-local work root was accepted")
