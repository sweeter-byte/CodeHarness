# Architecture

CodeHarness v1 is a local coding-agent runtime. One `CodeHarness` instance assembles the model client, leader agent, native and MCP tools, context and memory services, task graph, teammates, workflows, background jobs, cron scheduler, and goal controller. The terminal interface is the current user-facing entry point.

This document describes the checked-in implementation, not a distributed-agent platform or an operating-system sandbox.

## Runtime composition

The startup path is `main.py` → `interfaces/cli.py` → `CodeHarness.from_env()` → `CodeHarness.start()`. Configuration is loaded from the environment, including `.env`, and `codeharness/app.py` acts as the composition root.

| Layer | Current responsibility | Implementation evidence |
|---|---|---|
| CLI | Starts the runtime, accepts turns, exposes context/clear commands, and handles approvals | [`main.py`](../main.py), [`interfaces/cli.py`](../interfaces/cli.py) |
| Composition root | Creates the OpenAI-compatible client, tool registry, leader agent, stores, schedulers, Team, Workflow, MCP, and Goal Loop | [`codeharness/app.py`](../codeharness/app.py) |
| Agent core | Runs the model/tool loop, dispatches tool calls, recovers from context overflow, collects background results, and extracts leader memory | [`codeharness/core/agent.py`](../codeharness/core/agent.py) |
| Tool boundary | Defines native coding tools and routes every call through execution and permission checks | [`codeharness/tools/`](../codeharness/tools), [`codeharness/security/permission.py`](../codeharness/security/permission.py) |
| Context and memory | Budgets tokens, externalizes large outputs, archives transcripts, creates checkpoints, and stores project-scoped Markdown memories | [`codeharness/context/`](../codeharness/context), [`codeharness/memory/manager.py`](../codeharness/memory/manager.py) |
| Coordination | Persists a task DAG, runs session teammates, provides mailboxes, and optionally assigns Git worktrees | [`codeharness/tasks/`](../codeharness/tasks), [`codeharness/team/`](../codeharness/team) |
| Orchestration | Interprets registered workflow definitions, limits resources, records step journals, and resumes interrupted runs | [`codeharness/workflow/`](../codeharness/workflow) |
| External tools | Loads configured MCP servers, registers prefixed tools, and applies host-side permission policy | [`codeharness/mcp/`](../codeharness/mcp) |
| Evaluation | Runs isolated smoke cases or a single SWE-bench instance and records independent verification artifacts | [`evals/`](../evals), [evaluation notes](evaluation.md) |

## Execution modes

| Mode | Lifetime and state | Best fit | Current boundary |
|---|---|---|---|
| Agent Loop | Reuses leader conversation state across CLI turns in one runtime | Interactive exploration and implementation | One leader turn is serialized at a time |
| SubAgent | Fresh context for one delegated task; only the final response returns | Bounded investigation or implementation | Base tools only; no nested delegation, Team, Task-system, or MCP tools |
| Team | Independent agent threads share the task graph and file-backed mailboxes for the active runtime session | Multiple coordinated tasks | Teammates are daemon threads and are shut down with the runtime; they are not restored as live workers after process exit |
| Workflow | Static phases and typed steps with persisted snapshots and journals | Repeatable orchestration with explicit budgets | Local daemon-thread execution, not a distributed scheduler |

The Team task graph is file-backed and survives process restarts, but live teammate objects and their conversational state do not. A teammate can remain active across multiple assignments within one started runtime, which is the sense in which Team is session-persistent.

Workflow definitions support `AgentStep`, direct `ToolStep`, fork/join `ParallelStep`, per-item `PipelineStep`, and nested `WorkflowStep`. Successful step results are journaled and can be reused on resume. Definitions are registered in Python, conditions use a deliberately small expression evaluator, and parallel branches or pipeline stages accept only agent/tool steps. The engine enforces configured concurrency, agent-call, token, nesting-depth, and timeout limits. See [`definition.py`](../codeharness/workflow/definition.py), [`runtime.py`](../codeharness/workflow/runtime.py), and [`state.py`](../codeharness/workflow/state.py).

## Permission boundary

Native tool calls pass through `PermissionManager`, which applies three kinds of policy:

- resolved paths are compared with configured workspace/worktree roots;
- a deny list rejects selected destructive shell patterns, while an ask list requires interactive approval for selected mutations;
- MCP tools are authorized by a host-side allow/ask/deny table, with unknown tools defaulting to approval.

This is a best-effort application permission layer, not process isolation. Shell path extraction and command-pattern matching cannot model every shell construct, and approvals do not constrain a subprocess at the kernel level. Use containers, virtual machines, or another OS-level sandbox when executing untrusted code. The SWE-bench rollout path supplies its own Docker isolation; the normal CLI does not. Evidence: [`codeharness/security/permission.py`](../codeharness/security/permission.py) and [`evals/swebench/rollout_container.py`](../evals/swebench/rollout_container.py).

## Storage scopes

`RuntimePaths` derives a stable project ID from the Git common directory when available. Runtime files and cross-session memory intentionally use different roots.

| Path | Contents |
|---|---|
| `<workspace>/.codeharness/state/tasks/` | One JSON file per task in the persistent DAG |
| `<workspace>/.codeharness/state/scheduler/jobs.json` | Durable cron job definitions |
| `<workspace>/.codeharness/runs/workflows/<run-id>/` | Workflow snapshot, append-only journal, and final output |
| `<workspace>/.codeharness/runtime/team/mailboxes/` | File-backed Team inboxes |
| `<workspace>/.codeharness/artifacts/` | Externalized large tool outputs |
| `<workspace>/.codeharness/transcripts/` | Archived conversation segments used by context compaction |
| `<workspace>/.codeharness/worktrees/` | Optional Team Git worktrees |
| `<CODEHARNESS_HOME>/projects/<project-id>/memory/` | Project-scoped, cross-session leader memory |

The canonical mapping is in [`codeharness/paths.py`](../codeharness/paths.py). These directories are created lazily by their owners.

## MCP support

The configuration schema recognizes `stdio` and `streamable_http`, but the current client implementation connects only `stdio` servers. Selecting `streamable_http` reaches an explicit `NotImplementedError`; it must not be presented as supported transport. Environment variables are expanded only where declared in the server configuration, and only the explicit `env` mapping is passed to the MCP subprocess. See [`codeharness/mcp/config.py`](../codeharness/mcp/config.py) and [`codeharness/mcp/client.py`](../codeharness/mcp/client.py).

## Current architectural limits

- The runtime is local and single-process; concurrency is thread-based.
- Permission checks reduce accidental risk but provide no containment guarantee.
- Team workers and leader conversation history do not resume across process restarts.
- Workflow state is locally resumable, but execution is neither remotely scheduled nor replicated.
- MCP transport is currently stdio-only.
- SWE-bench support handles one instance per invocation; batching, retries, pass@k, and leaderboard orchestration are outside v1.
