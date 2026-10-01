# Workflow Runtime Concurrency Design

## Scope

This change is limited to Workflow Runtime in-process state synchronization,
Agent-local token usage, snapshot write serialization, and cancel/shutdown
lifecycle correctness. It preserves the existing ToolExecutor boundary and
Runtime-owned workflow components. Resume locking, structured output semantics,
builtin workflows, delivery, Goal Loop, and unrelated workflow behavior remain
out of scope.

## Run execution state

Each `RunHandle` owns one `threading.RLock`, one cancellation event, and the
single in-memory `RunSnapshot` for that run. All live-run snapshot changes use
the handle's snapshot rather than loading replacement objects from disk.

The run lock covers only short state transactions:

1. check guards or cancellation;
2. reserve or mutate counters/status;
3. persist the snapshot and index;
4. release the lock.

Agent loops, LLM calls, tool execution, waits, delivery, and event callbacks do
not execute while the run lock is held. If persistence acquires a store lock,
the fixed order is `RunHandle.state_lock` followed by the StateStore per-run
`RLock`; no code acquires these in reverse order.

## Agent-call and token accounting

A journal cache hit returns before reservation and consumes no new Agent call.
After an Agent is constructed and immediately before `agent_loop()` starts, a
locked reservation checks `agent_calls_used` and the current token usage guard,
increments `agent_calls_used`, and persists it. Therefore each genuinely
started AgentStep attempt consumes one call even when it later fails.

Token budget enforcement is a usage guard, not a strict reservation quota.
Concurrent calls may collectively exceed the budget because their usage is not
known beforehand. Once accounted usage reaches the limit, later AgentSteps do
not start. Each attempt's actual local usage is added exactly once under the run
lock after the Agent finishes or raises.

## Agent statistics

Every Agent always owns a distinct `local_stats` object. Its `session_stats`
object is a separate aggregate: injected Runtime statistics when available, or
a second Agent-created object for a standalone Agent. `_accumulate_tokens()`
always increments local statistics and then increments aggregate statistics
under the aggregate object's lock. There is no identity-based aliasing or
double-count avoidance branch.

Workflow accounting reads only `agent.local_stats`, so pre-existing Runtime
usage cannot leak into a Workflow step. Multi-round and structured-output retry
usage naturally accumulates through the same Agent instance.

## StateStore writes

`WorkflowStateStore` uses one `threading.RLock` per run. Snapshot saves and
journal appends for the same run serialize through that lock, while different
runs remain independent. Snapshot mutation remains the Runtime's responsibility;
the store lock protects file operations and the shared temporary filename.

## Cancellation and shutdown

`WorkflowCancelled` is distinct from execution failures. User cancellation and
shutdown set the cancellation event and persist `CANCELLED` under the run lock.
Workers check cancellation after long-running work returns and before any
terminal `COMPLETED` or `FAILED` transition. Explicit cancellation therefore
wins over a late result or late exception. Genuine exceptions without a cancel
request remain `FAILED`.

`shutdown_all()` cancels a snapshot of handles, waits until a shared deadline,
and removes only handles whose threads have stopped. Live handles remain in
`_runs`, so a later close can wait again without releasing Runtime dependencies.
Workers do not remove their own handles. Public Runtime operations prune only
handles whose threads are confirmed dead, preventing unbounded terminal-handle
growth without creating an early-removal race.

## Verification

Tests cover parallel call counting, concurrent budget reservation, parallel
token totals, local-versus-aggregate usage, Agent dual accounting, concurrent
snapshot saves, retryable shutdown, cancellation terminal state, genuine
failure terminal state, and the existing lifecycle regression suite. The full
pytest suite is run last, with unrelated known-scope failures reported rather
than repaired.
