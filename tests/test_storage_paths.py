"""Storage scopes are explicit and independent of the process cwd."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_runtime_paths_describe_project_and_agent_scopes(tmp_path):
    from codeharness.paths import RuntimePaths

    workspace = tmp_path / "workspace"
    agent_home = tmp_path / "agent-home"
    workspace.mkdir()

    paths = RuntimePaths.build(workspace=workspace, agent_home=agent_home)

    assert paths.workspace == workspace.resolve()
    assert paths.agent_home == agent_home.resolve()
    assert paths.project_root == workspace / ".codeharness"
    assert paths.state_root == workspace / ".codeharness/state"
    assert paths.tasks_dir == workspace / ".codeharness/state/tasks"
    assert paths.scheduler_dir == workspace / ".codeharness/state/scheduler"
    assert paths.scheduler_jobs_file == workspace / ".codeharness/state/scheduler/jobs.json"
    assert paths.runs_root == workspace / ".codeharness/runs"
    assert paths.workflow_runs_dir == workspace / ".codeharness/runs/workflows"
    assert paths.runtime_root == workspace / ".codeharness/runtime"
    assert paths.team_runtime_dir == workspace / ".codeharness/runtime/team"
    assert paths.mailboxes_dir == workspace / ".codeharness/runtime/team/mailboxes"
    assert paths.artifacts_dir == workspace / ".codeharness/artifacts"
    assert paths.transcripts_dir == workspace / ".codeharness/transcripts"
    assert paths.worktrees_dir == workspace / ".codeharness/worktrees"
    assert paths.project_home == agent_home / "projects" / paths.project_id
    assert paths.project_memory_dir == paths.project_home / "memory"
    assert paths.global_memory_dir == agent_home / "memory"
    assert not paths.project_root.exists()
    assert not paths.agent_home.exists()


def test_runtime_paths_non_git_identity_is_stable(tmp_path):
    from codeharness.paths import RuntimePaths

    workspace = tmp_path / "plain-project"
    workspace.mkdir()

    first = RuntimePaths.build(workspace=workspace, agent_home=tmp_path / "home-a")
    second = RuntimePaths.build(workspace=workspace / ".", agent_home=tmp_path / "home-b")

    assert first.project_id == second.project_id
    assert first.project_id.startswith("plain-project-")


def test_runtime_paths_git_worktrees_share_project_id(tmp_path):
    from codeharness.paths import RuntimePaths

    repository = tmp_path / "shared-project"
    linked = tmp_path / "linked-worktree"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Test User"],
        check=True,
    )
    (repository / "README.md").write_text("test\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "initial"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "worktree", "add", "-q", str(linked), "-b", "linked"],
        check=True,
    )

    main_paths = RuntimePaths.build(repository, tmp_path / "agent-home")
    linked_paths = RuntimePaths.build(linked, tmp_path / "agent-home")

    assert main_paths.project_id == linked_paths.project_id
    assert main_paths.project_id.startswith("shared-project-")


def test_runtime_config_reads_agent_home_default_and_override(monkeypatch, tmp_path):
    import codeharness.config as config_module

    monkeypatch.setattr(config_module, "load_dotenv", lambda **kwargs: None)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("DEEPSEEK_MODEL_ID", "model")
    monkeypatch.setenv("WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.delenv("CODEHARNESS_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))

    default = config_module.RuntimeConfig.from_env()
    assert default.agent_home == (tmp_path / "user-home/.codeharness").resolve()

    override = tmp_path / "custom-home"
    monkeypatch.setenv("CODEHARNESS_HOME", str(override))
    configured = config_module.RuntimeConfig.from_env()
    assert configured.agent_home == override.resolve()


def test_file_stores_create_only_their_configured_paths(tmp_path, monkeypatch):
    from codeharness.context.artifact_store import ArtifactStore
    from codeharness.context.transcript_store import TranscriptStore
    from codeharness.scheduler.cron import CronStore
    from codeharness.tasks.store import TaskStore
    from codeharness.team.bus import MessageBus

    workspace = tmp_path / "workspace"
    unrelated_cwd = tmp_path / "cwd"
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)

    task_store = TaskStore(workspace / ".codeharness/state/tasks")
    task = task_store.create("storage test")
    assert (task_store.tasks_dir / f"{task.id}.json").exists()

    cron_store = CronStore(workspace / ".codeharness/state/scheduler/jobs.json")
    cron_store.add("* * * * *", "durable", recurring=True, durable=True)
    assert cron_store.path.exists()

    bus = MessageBus(workspace / ".codeharness/runtime/team/mailboxes")
    bus.send("lead", "Alice", "hello")
    assert (bus.mailbox_dir / "Alice.jsonl").exists()

    artifact_store = ArtifactStore(workspace / ".codeharness/artifacts")
    artifact_id = artifact_store.save("artifact")
    assert (artifact_store.base_dir / f"{artifact_id}.txt").exists()

    transcript_store = TranscriptStore(workspace / ".codeharness/transcripts")
    transcript_id = transcript_store.save([{"role": "user", "content": "hello"}])
    assert (transcript_store.base_dir / f"{transcript_id}.jsonl").exists()

    for legacy in (
        ".tasks",
        ".memory",
        ".mailboxes",
        ".worktrees",
        ".scheduled_tasks.json",
        ".codeharness/workflows",
    ):
        assert not (unrelated_cwd / legacy).exists()


def test_memory_manager_custom_directory_owns_index(tmp_path, monkeypatch):
    from codeharness.memory.manager import MemoryManager

    unrelated_cwd = tmp_path / "cwd"
    memory_dir = tmp_path / "agent-home/projects/project-id/memory"
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)

    manager = MemoryManager(memory_dir=memory_dir)
    manager.write_memory_file("Useful Fact", "project", "description", "body")

    assert manager.index_path == memory_dir / "MEMORY.md"
    assert manager.index_path.exists()
    assert not (unrelated_cwd / ".memory").exists()


def test_worktree_configuration_roots_paths_and_unbound_tasks(tmp_path, monkeypatch):
    from codeharness.team import worktree
    from codeharness.tasks.store import Task

    workspace = tmp_path / "workspace"
    worktrees_dir = workspace / ".codeharness/worktrees"
    monkeypatch.setattr(worktree, "_workspace_root", worktree._workspace_root)
    monkeypatch.setattr(worktree, "_worktrees_dir", worktree._worktrees_dir)
    worktree.configure_worktrees(workspace, worktrees_dir)
    task = Task("task_1", "subject", "", "pending", None)

    assert worktree.worktree_path("Alice") == worktrees_dir / "Alice"
    assert worktree.resolve_worktree_cwd(task) == (str(workspace.resolve()), None)
    assert not worktrees_dir.exists()


def test_runtime_composes_all_paths_and_shared_artifact_store(monkeypatch, tmp_path):
    from codeharness import app as app_module
    from codeharness.config import RuntimeConfig
    from codeharness.context.artifact_store import ARTIFACT_STORE
    from codeharness.context.transcript_store import TRANSCRIPT_STORE
    from codeharness.tasks import TASKS
    from codeharness.team.bus import BUS
    from codeharness.team import worktree

    workspace = tmp_path / "workspace"
    agent_home = tmp_path / "agent-home"
    unrelated_cwd = tmp_path / "cwd"
    workspace.mkdir()
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)

    monkeypatch.setattr(TASKS, "_tasks_dir", TASKS._tasks_dir)
    monkeypatch.setattr(BUS, "_mailbox_dir", BUS._mailbox_dir)
    monkeypatch.setattr(ARTIFACT_STORE, "_base_dir", ARTIFACT_STORE._base_dir)
    monkeypatch.setattr(TRANSCRIPT_STORE, "_base_dir", TRANSCRIPT_STORE._base_dir)
    monkeypatch.setattr(worktree, "_workspace_root", None)
    monkeypatch.setattr(worktree, "_worktrees_dir", None)
    monkeypatch.setattr(app_module, "OpenAI", lambda **kwargs: object())
    monkeypatch.setattr(app_module, "load_config", lambda path: {})
    monkeypatch.setattr(app_module, "MCPManager", lambda configs: _FakeMCPManager())
    monkeypatch.setattr(app_module, "shutdown_runtime", lambda: None)
    monkeypatch.setattr(app_module.team_wakeup, "start", lambda **kwargs: None)
    monkeypatch.setattr(app_module.team_wakeup, "stop", lambda: True)
    monkeypatch.setattr(app_module.cron, "stop", lambda: True)
    cron_calls = []
    monkeypatch.setattr(app_module.cron, "start", lambda **kwargs: cron_calls.append(kwargs))

    config = RuntimeConfig(
        api_key="key",
        base_url="https://example.invalid",
        model="model",
        model_context_window=4096,
        workspace=workspace,
        mcp_config_path=workspace / "mcp.json",
        agent_home=agent_home,
    )
    harness = app_module.CodeHarness(config).start()

    assert harness.workflow_state_store.base_dir == harness.paths.workflow_runs_dir
    assert TASKS.tasks_dir == harness.paths.tasks_dir
    assert BUS.mailbox_dir == harness.paths.mailboxes_dir
    assert ARTIFACT_STORE.base_dir == harness.paths.artifacts_dir
    assert TRANSCRIPT_STORE.base_dir == harness.paths.transcripts_dir
    assert worktree.worktree_path("Alice") == harness.paths.worktrees_dir / "Alice"
    assert cron_calls[0]["store_path"] == harness.paths.scheduler_jobs_file
    assert harness.agent.memory_manager.memory_dir == harness.paths.project_memory_dir
    assert harness.agent.context_manager.artifact_store is ARTIFACT_STORE
    assert not workspace.joinpath(".codeharness").exists()
    assert not agent_home.exists()

    from codeharness.context.tools import run_read_artifact
    artifact_id = harness.agent.context_manager.artifact_store.save("shared content")
    assert run_read_artifact(artifact_id) == "shared content"
    assert (harness.paths.artifacts_dir / f"{artifact_id}.txt").exists()
    assert harness.close() is True


class _FakeMCPManager:
    def connect_all(self):
        pass

    def status_lines(self):
        return []

    def assemble(self, names):
        return [], {}

    def resolve(self, prefixed):
        return None

    def annotations_of(self, prefixed):
        return None

    def close_all(self):
        pass


def test_workflow_store_is_lazy_and_writes_under_runs_directory(tmp_path):
    from codeharness.workflow.state import RunSnapshot, WorkflowStateStore

    base_dir = tmp_path / "workspace/.codeharness/runs/workflows"
    store = WorkflowStateStore(base_dir)
    assert not base_dir.exists()

    store.save_snapshot(RunSnapshot(run_id="run_1", workflow_name="test", inputs={}))

    assert (base_dir / "run_1/snapshot.json").exists()


def test_importing_runtime_modules_has_no_storage_side_effects(tmp_path):
    workspace = tmp_path / "workspace"
    agent_home = tmp_path / "agent-home"
    process_cwd = tmp_path / "cwd"
    workspace.mkdir()
    process_cwd.mkdir()
    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(repo_root),
        "WORKSPACE": str(workspace),
        "CODEHARNESS_HOME": str(agent_home),
    })

    subprocess.run(
        [sys.executable, "-c", "import codeharness; import codeharness.app"],
        cwd=process_cwd,
        env=env,
        check=True,
    )

    assert not (process_cwd / ".tasks").exists()
    assert not (process_cwd / ".memory").exists()
    assert not (process_cwd / ".mailboxes").exists()
    assert not (process_cwd / ".worktrees").exists()
    assert not (process_cwd / ".scheduled_tasks.json").exists()
    assert not (process_cwd / ".codeharness").exists()
    assert not (workspace / ".codeharness").exists()
    assert not agent_home.exists()
