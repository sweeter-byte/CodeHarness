# Workflow Runtime Concurrency Implementation Plan

> **For agentic workers:** Execute inline with `superpowers:test-driven-development`; do not commit automatically.

**Goal:** Make Workflow Runtime counters, usage accounting, snapshot writes,
cancellation, and shutdown correct under Parallel/Pipeline concurrency.

**Architecture:** A `RunHandle` owns the run's sole snapshot and an `RLock`.
Short state transactions mutate and persist through that lock, while Agent and
tool work executes outside it. Agent-local and Runtime-aggregate statistics are
separate lock-aware objects.

**Tech Stack:** Python, threading, dataclasses, pytest.

---

### Task 1: Add regression tests for statistics and store serialization

**Files:**
- Modify: `tests/test_phase5b_runtime_state.py`
- Create: `tests/test_workflow_runtime_concurrency.py`

- [ ] Add a test whose fake response reports prompt=10, completion=5, total=15;
  assert the Agent's distinct `local_stats` and injected `session_stats` both
  contain those deltas.
- [ ] Add a multithreaded `WorkflowStateStore.save_snapshot()` test using one
  run id; assert no worker raises and the final snapshot is valid JSON.
- [ ] Run the two tests and verify they fail because `local_stats` and snapshot
  write locking do not yet exist.

### Task 2: Add Workflow concurrency and budget regression tests

**Files:**
- Modify: `tests/test_workflow_runtime_concurrency.py`

- [ ] Build a real `WorkflowRuntime` fixture with fake Agents synchronized by
  barriers/events and distinct local token totals.
- [ ] Add a ParallelStep test proving every started attempt is reflected in
  `agent_calls_used`.
- [ ] Add a four-branch/max-two-calls test proving no more than two Agents start
  and the run exposes `WorkflowBudgetExceeded` behavior.
- [ ] Add 100/200/300-token parallel tests proving the snapshot total is 600.
- [ ] Seed aggregate usage at 10000 and prove a 200-token Workflow Agent adds
  only 200 to the Workflow snapshot.
- [ ] Run these tests and verify the expected counter/budget/usage failures.

### Task 3: Add cancellation and retryable-shutdown regression tests

**Files:**
- Modify: `tests/test_workflow_runtime_concurrency.py`

- [ ] Add a blocking worker test: short shutdown returns false and retains the
  live handle; after release, retry returns true and removes it.
- [ ] Add a blocking Agent test: `cancel()` followed by a late Agent return ends
  as `CANCELLED`, never `COMPLETED` or `FAILED`.
- [ ] Add a throwing Agent test without cancellation and assert final `FAILED`.
- [ ] Run the lifecycle tests and verify they fail for the current clear-all and
  generic-exception behavior.

### Task 4: Implement distinct local and aggregate Agent statistics

**Files:**
- Modify: `codeharness/hooks.py`
- Modify: `codeharness/core/agent.py`

- [ ] Introduce a dict-compatible session-stat object with an internal lock and
  an atomic add method; retain all existing keys and mapping behavior.
- [ ] Always create a separate `local_stats`; create or retain a separate
  `session_stats` aggregate.
- [ ] Change `_accumulate_tokens()` to add each usage field to both objects,
  locking aggregate updates via its atomic method.
- [ ] Run Agent state tests and verify local and aggregate assertions pass.

### Task 5: Implement per-run Runtime state transactions

**Files:**
- Modify: `codeharness/workflow/runtime.py`

- [ ] Add `state_lock: threading.RLock` to `RunHandle` and make the worker use
  `handle.snapshot` as the run's unique live snapshot.
- [ ] Add focused helpers for handle lookup, dead-handle pruning, cancellation
  checks, snapshot persistence, Agent-call reservation, usage recording, and
  terminal transitions.
- [ ] Under the run lock, make journal lookup plus call/token guards precede an
  atomic call reservation and persistence; release before `agent_loop()`.
- [ ] In a finally-style accounting path, add `agent.local_stats.total_tokens`
  exactly once; on success also mutate `completed_steps` and journal state.
- [ ] Protect phase, nested-usage, completed, failed, and cancelled snapshot
  mutations with the same run lock.
- [ ] Run concurrency tests and verify call and token tests pass without
  serializing the fake Agents.

### Task 6: Implement StateStore per-run file serialization

**Files:**
- Modify: `codeharness/workflow/state.py`

- [ ] Change the per-run lock map to `threading.RLock`.
- [ ] Acquire it around same-run snapshot serialization, temporary-file write,
  and replace; retain it around journal append.
- [ ] Run the concurrent-save regression test and verify valid final JSON and no
  temporary-file race.

### Task 7: Implement cancellation and retryable handle cleanup

**Files:**
- Modify: `codeharness/workflow/runtime.py`

- [ ] Add `WorkflowCancelled` and use explicit cancellation checks rather than
  matching exception text.
- [ ] Make `cancel()` set the event and persist `CANCELLED` under the run lock.
- [ ] Recheck cancellation after Agent/step return and before every terminal
  transition; cancellation wins over late success or exception.
- [ ] Make `shutdown_all()` retain live handles and remove only confirmed-dead
  handles; safely prune dead handles from public operations.
- [ ] Run cancel/failure/shutdown tests and verify all terminal states and retry
  behavior.

### Task 8: Regression and full verification

**Files:**
- Verify only; no planned source changes.

- [ ] Run `tests/test_final_fix2_shutdown.py`,
  `tests/test_phase5b_runtime_state.py`,
  `tests/test_workflow_runtime_ownership.py`, and
  `tests/test_tool_execution_boundary.py` together.
- [ ] Run the complete pytest suite.
- [ ] Run `git diff --check`, inspect `git status --short`, and capture
  `git diff --stat`.
- [ ] Compare the diff against every requirement and confirm no out-of-scope
  subsystem changed.
