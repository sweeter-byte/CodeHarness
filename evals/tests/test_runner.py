import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import evals.runner.runner as runner_module
from evals.runner.case import EvalCase, Verification
from evals.runner.runner import (
    _worker_environment,
    build_run_config,
    run_case,
    run_evaluation,
    write_json,
)
from evals.runner.workspace import WorkspaceManager


REPO_ROOT = Path(__file__).parents[2]


def _case(tmp_path, verifier=None, timeout=3):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "value.txt").write_text("broken\n", encoding="utf-8")
    source = tmp_path / "case.json"
    source.write_text("{}")
    return EvalCase(
        case_id="sample",
        category="bugfix",
        description="sample",
        task="Fix it.",
        fixture_dir=fixture,
        timeout_seconds=timeout,
        verification=Verification(
            tuple(
                verifier
                or [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; "
        "assert Path('value.txt').read_text() == 'fixed\\n'; "
                ]
            )
        ),
        source_path=source,
    )


def _manager(tmp_path, run_id):
    repo = tmp_path / "source-repo"
    repo.mkdir(exist_ok=True)
    return WorkspaceManager(
        run_id=run_id,
        work_root=tmp_path / "work",
        repository_root=repo,
    )


def _successful_worker(secret):
    code = (
        "import json, sys; "
        "from pathlib import Path; "
        "Path('value.txt').write_text('fixed\\n'); "
        "Path('secret.txt').write_text('" + secret + "'); "
        "Path(sys.argv[1]).write_text(json.dumps({"
        "'final_answer': '" + secret + " says all tests passed',"
        "'session_stats': {'prompt_tokens': 2, 'completion_tokens': 3,"
        "'total_tokens': 5, 'tool_calls': 1},"
        "'worker_status': 'completed', 'error': None})); "
        "print('" + secret + " stdout'); "
        "print('" + secret + " stderr', file=sys.stderr)"
    )

    def factory(result_path):
        return [sys.executable, "-c", code, str(result_path)]

    return factory


def test_run_case_uses_independent_verifier_and_persists_redacted_artifacts(
    tmp_path,
):
    secret = "eval-secret-value"
    run_id = "run-success"
    result_root = tmp_path / "results" / run_id
    verifier_code = (
        "import sys; "
        "from pathlib import Path; "
        "assert Path('value.txt').read_text() == 'fixed\\n'; "
        f"print('{secret} verifier stdout'); "
        f"print('{secret} verifier stderr', file=sys.stderr)"
    )
    case = _case(tmp_path, verifier=[sys.executable, "-c", verifier_code])
    result = run_case(
        case,
        run_id=run_id,
        workspace_manager=_manager(tmp_path, run_id),
        result_root=result_root,
        environment={"DEEPSEEK_API_KEY": secret},
        worker_command_factory=_successful_worker(secret),
    )

    case_dir = result_root / "cases" / "sample"
    assert result["success"] is True
    assert result["timeout"] is False
    assert result["worker_status"] == "completed"
    assert result["verifier_exit_code"] == 0
    assert result["total_tokens"] == 5
    assert result["tool_calls"] == 1
    assert Path(result["workspace_path"]).is_dir()
    assert "+fixed" in (case_dir / "patch.diff").read_text()
    for name in (
        "result.json",
        "agent.stdout.log",
        "agent.stderr.log",
        "verifier.stdout.log",
        "verifier.stderr.log",
        "patch.diff",
    ):
        assert secret not in (case_dir / name).read_text()
    assert "***REDACTED***" in (case_dir / "agent.stdout.log").read_text()


def test_worker_crash_is_failure_even_when_verifier_passes(tmp_path):
    run_id = "run-crash"
    result = run_case(
        _case(
            tmp_path,
            verifier=[sys.executable, "-c", "raise SystemExit(0)"],
        ),
        run_id=run_id,
        workspace_manager=_manager(tmp_path, run_id),
        result_root=tmp_path / "results" / run_id,
        worker_command_factory=lambda _: [
            sys.executable,
            "-c",
            "raise SystemExit(7)",
        ],
    )

    assert result["success"] is False
    assert result["timeout"] is False
    assert result["worker_status"] == "crashed"
    assert result["worker_error"] == "worker exited with code 7"
    assert result["verifier_exit_code"] == 0


def _wait_until_process_gone(pid, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        status = Path(f"/proc/{pid}/status")
        if status.exists() and "\nState:\tZ" in status.read_text():
            return True
        time.sleep(0.02)
    return False


def test_worker_timeout_fails_and_kills_descendant_process(tmp_path):
    run_id = "run-timeout"
    helper = Path(__file__).parent / "helpers" / "process_tree.py"
    result = run_case(
        _case(
            tmp_path,
            verifier=[sys.executable, "-c", "raise SystemExit(0)"],
            timeout=1,
        ),
        run_id=run_id,
        workspace_manager=_manager(tmp_path, run_id),
        result_root=tmp_path / "results" / run_id,
        worker_command_factory=lambda _: [sys.executable, str(helper)],
    )

    child_pid = int(
        (Path(result["workspace_path"]) / "child.pid").read_text()
    )
    assert result["success"] is False
    assert result["timeout"] is True
    assert result["worker_status"] == "timeout"
    assert _wait_until_process_gone(child_pid), f"descendant {child_pid} survived"


def test_run_config_never_contains_api_key(tmp_path):
    secret = "config-secret"
    environment = {
        "DEEPSEEK_API_KEY": secret,
        "DEEPSEEK_MODEL_ID": "model-x",
        "DEEPSEEK_BASE_URL": "https://models.example:8443/v1",
    }

    config = build_run_config(
        run_id="run-config",
        suite="smoke",
        case_ids=["a", "b"],
        repository_root=REPO_ROOT,
        environment=environment,
    )
    path = tmp_path / "config.json"
    write_json(path, config, environment)

    raw = path.read_text()
    assert secret not in raw
    assert config["model"] == "model-x"
    assert config["base_url"] == "models.example:8443"
    assert config["case_ids"] == ["a", "b"]


def test_summary_json_redacts_api_key(tmp_path):
    secret = "summary-secret-value"
    path = tmp_path / "summary.json"

    write_json(
        path,
        {"failure_details": [{"error": f"provider rejected {secret}"}]},
        {"DEEPSEEK_API_KEY": secret},
    )

    raw = path.read_text(encoding="utf-8")
    assert secret not in raw
    assert "***REDACTED***" in raw


def test_load_evaluation_environment_reads_only_repository_dotenv(
    tmp_path, monkeypatch
):
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    monkeypatch.setattr(runner_module, "REPOSITORY_ROOT", repository_root)
    (repository_root / ".env").write_text(
        "DEEPSEEK_API_KEY=repository-key\n"
        "DEEPSEEK_BASE_URL=https://repository.example/v1\n"
        "DEEPSEEK_MODEL_ID=repository-model\n",
        encoding="utf-8",
    )
    other_directory = tmp_path / "other"
    other_directory.mkdir()
    (other_directory / ".env").write_text(
        "DEEPSEEK_API_KEY=cwd-key\n",
        encoding="utf-8",
    )
    for name in (
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "DEEPSEEK_MODEL_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(other_directory)

    environment = runner_module.load_evaluation_environment()

    assert environment["DEEPSEEK_API_KEY"] == "repository-key"
    assert environment["DEEPSEEK_BASE_URL"] == "https://repository.example/v1"
    assert environment["DEEPSEEK_MODEL_ID"] == "repository-model"


def test_load_evaluation_environment_preserves_exported_values(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(runner_module, "REPOSITORY_ROOT", tmp_path)
    (tmp_path / ".env").write_text(
        "DEEPSEEK_API_KEY=repository-key\n"
        "DEEPSEEK_BASE_URL=https://repository.example/v1\n"
        "DEEPSEEK_MODEL_ID=repository-model\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "shell-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://shell.example/v1")
    monkeypatch.setenv("DEEPSEEK_MODEL_ID", "shell-model")

    environment = runner_module.load_evaluation_environment()

    assert environment["DEEPSEEK_API_KEY"] == "shell-key"
    assert environment["DEEPSEEK_BASE_URL"] == "https://shell.example/v1"
    assert environment["DEEPSEEK_MODEL_ID"] == "shell-model"


def test_run_evaluation_fails_before_creating_run_artifacts(
    tmp_path, monkeypatch
):
    results_root = tmp_path / "eval-results"
    run_id_created = False

    def unexpected_make_run_id():
        nonlocal run_id_created
        run_id_created = True
        return "unexpected"

    monkeypatch.setattr(runner_module, "make_run_id", unexpected_make_run_id)

    with pytest.raises(
        runner_module.EvaluationConfigurationError,
        match=(
            r"Missing evaluation model configuration:\n"
            r"  DEEPSEEK_API_KEY\n"
            r"  DEEPSEEK_MODEL_ID\n\n"
            r"Configure it in:\n"
        ),
    ):
        run_evaluation(
            suite="smoke",
            results_root=results_root,
            environment={"DEEPSEEK_BASE_URL": "https://models.example/v1"},
        )

    assert run_id_created is False
    assert not results_root.exists()


def test_main_reports_missing_configuration_without_traceback(
    monkeypatch, capsys
):
    error = runner_module.EvaluationConfigurationError("missing config")

    def fail_evaluation(**kwargs):
        raise error

    monkeypatch.setattr(runner_module, "run_evaluation", fail_evaluation)

    exit_code = runner_module.main(["--suite", "smoke"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert captured.err == "missing config\n"


def test_importing_parent_runner_does_not_import_codeharness_runtime():
    code = (
        "import sys; import evals.runner.runner; "
        "print('codeharness.app' in sys.modules)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    assert completed.stdout.strip() == "False"


def test_worker_environment_forces_empty_case_local_mcp_config(tmp_path):
    workspace = tmp_path / "workspace"
    agent_home = tmp_path / "agent-home"
    workspace.mkdir()
    agent_home.mkdir()
    host_mcp_config = tmp_path / "host-mcp.json"

    child_env = _worker_environment(
        workspace,
        agent_home,
        {
            "WORKSPACE": str(tmp_path / "dotenv-workspace"),
            "CODEHARNESS_HOME": str(tmp_path / "dotenv-agent-home"),
            "MCP_CONFIG_PATH": str(host_mcp_config),
        },
    )

    case_mcp_config = agent_home / "mcp/servers.json"
    assert child_env["WORKSPACE"] == str(workspace)
    assert child_env["CODEHARNESS_HOME"] == str(agent_home)
    assert child_env["MCP_CONFIG_PATH"] == str(case_mcp_config)
    assert case_mcp_config.read_text(encoding="utf-8") == "{}\n"
    assert not host_mcp_config.exists()
