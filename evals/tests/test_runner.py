import json
import os
import subprocess
import sys
import time
from pathlib import Path

from evals.runner.case import EvalCase, Verification
from evals.runner.runner import (
    _worker_environment,
    build_run_config,
    run_case,
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
                    "assert Path('value.txt').read_text() == 'fixed\\n'",
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
    result = run_case(
        _case(tmp_path),
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
        {"MCP_CONFIG_PATH": str(host_mcp_config)},
    )

    case_mcp_config = agent_home / "mcp/servers.json"
    assert child_env["MCP_CONFIG_PATH"] == str(case_mcp_config)
    assert case_mcp_config.read_text(encoding="utf-8") == "{}\n"
    assert not host_mcp_config.exists()
