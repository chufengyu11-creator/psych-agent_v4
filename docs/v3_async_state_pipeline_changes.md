# v3 Async State Pipeline Change Log

Date: 2026-08-04
Updated: 2026-08-05
Scope: /data/agent/psych-agent/psych-agent_v3 only

> Phase 1 below is retained as historical context.
> The current persisted N+1 behavior is documented in Phase 2 at the end.

## Phase 1 safety boundary (historical)

- psych-agent_v2 was not modified.
- No database migration or table change was made.
- The 27B and 0.8B model servers were not restarted or reconfigured.
- The new pipeline is disabled by default and is enabled only with
  ASYNC_STATE_PIPELINE_ENABLED=true.
- Deep-state output is shadow-only in this phase. It is measured and logged but
  does not mutate or persist SessionState.

## Code changes

- schemas/state.py
  - Added current_turn_goal to SessionState and StateDelta.
- agents/state_tracker.py
  - Added FastStateTracker for deterministic, high-confidence current-turn
    extraction.
  - Fast extraction recognizes explicit current goals, explicit session-goal
    changes, explicit emotions, and direct "not A but B" corrections.
  - Added extract_deep_delta for supervised background model analysis.
- services/state_reducer.py
  - Applies user corrections before other state updates.
  - Deactivates replaced values while preserving evidence history.
  - Prevents a negated old emotion from being reactivated in the same turn.
- orchestrator/deep_state_pipeline.py
  - Added an application-owned background queue.
  - Copies StateTrackerInput deeply before scheduling.
  - Preserves per-session order and limits global deep inference concurrency to
    one.
  - Consumes task failures and records counts without logging user content.
- orchestrator/turn_orchestrator.py
  - Uses FastStateTracker in the foreground when enabled.
  - Starts shadow deep analysis after strategy planning, before response
    generation.
- runtime/sqlalchemy_orchestrator.py
  - Passes fast/deep dependencies into each transaction-local orchestrator.
  - Does not share an AsyncSession with background work.
- runtime/factory.py
  - Builds one deep queue and shares its lifecycle with the application.
  - Makes post-turn summary wait for the same session's deep task.
- runtime/application.py
  - Drains deep work, then post-turn work, before closing HTTP/database
    resources.
- app/config.py
  - Added async_state_pipeline_enabled with a default of false.
- services/timing.py and existing call sites
  - Retain per-stage timing logs for before/after measurement.

## Execution order

Foreground:
feedback -> (risk || fast state) -> reduce/save state -> memory -> strategy ->
enqueue deep shadow -> response -> output guard -> commit -> return

Background:
deep shadow (per-session ordered, global concurrency 1) -> post-turn summary

## Risk controls

- Late N result cannot overwrite N+1 because shadow results are not persisted.
- SQLAlchemy AsyncSession is never passed to the background queue.
- Summary waits for deep analysis for the same session.
- Global deep concurrency is capped at one to limit 0.8B GPU contention.
- Explicit user corrections are applied before additions.
- Background failures are consumed, counted, and do not fail the user response.
- Application shutdown drains owned background tasks.

## Validation

- 6 dedicated fast/deep pipeline tests passed.
- 19 adjacent regression tests passed.
- Static patch and Python compilation checks passed.
- Full suite result in the current environment: 519 passed, 21 failed,
  65 errors. The reproduced failures enter database setup before this feature
  and are blocked by missing aiosqlite and an unavailable/failing configured
  PostgreSQL test migration. No dependency was installed and no test database
  migration was attempted manually.

## Problems encountered while editing

- A missing MessageId import was found and fixed.
- Negated emotions could be re-added by keyword extraction; exclusions were
  added and covered by a regression test.
- A patch initially matched a commented legacy copy instead of live code; the
  patch was rejected before application and regenerated against the live
  occurrence.
- An ApplicationRuntime indentation defect was caught by import checking and
  fixed before service restart.
- The first restarted v3 tmux session later disappeared during final cleanup.
  Its pane was not configured to remain after exit, so the exact exit cause
  could not be recovered. The service was recreated with remain-on-exit enabled
  and passed a subsequent stability check.
- SSH connections reset repeatedly; writes were checked after each disconnect
  before retrying.
- One newly created test file was corrupted by a non-UTF-8 transfer helper. It
  was confirmed untracked, removed, rebuilt as ASCII with Python Unicode
  escapes, and then compiled and tested.

## Activation and rollback

Activation for v3:
ASYNC_STATE_PIPELINE_ENABLED=true

Rollback:
restart v3 without that environment variable (or set it to false). The legacy
model-backed foreground state tracker is then used. No database rollback is
needed.

## Live measurements

The current v3 process is PID 1329687 with the feature flag enabled.

Turn 1, fresh session:
- Client-observed latency: 15.95 s.
- Server chat-turn latency: 15.79 s.
- Risk plus fast state: 0.832 s; fast state itself: 0.202 ms.
- Strategy: 9.43 s and used its existing truncation fallback.
- Deep shadow: 0.984 s.
- Response: 3.24 s.
- Output guard: 2.10 s.
- Deep shadow and response began concurrently; deep completed before response.
- No rolling summary was required on the first turn.

Turn 2, explicit correction:
- Client-observed latency: 11.17 s.
- Server chat-turn latency: 10.70 s.
- Feedback: 1.44 s.
- Risk plus fast state: 0.735 s; fast state itself: 0.229 ms.
- Strategy: 1.90 s.
- Deep shadow: 8.91 s; its 0.8B structured output was truncated.
- Response: 4.04 s.
- Output guard: 2.55 s.
- The deep failure was consumed and did not fail the foreground response.
- Post-turn summary waited for deep completion, then ran for 6.62 s. Its
  structured output also failed, without affecting the returned reply.
- Post-turn background duration after dispatch was 8.95 s.

Persisted fast-state verification after turn 2:
- state version: 2
- old emotion "pressure": inactive
- corrected emotion "angry": active
- current-turn goal and session goal reflect the explicit second-turn request

Observed result:
- The old model-backed foreground state stage, previously approximately
  8-9 seconds, was reduced to the risk duration (approximately 0.7-0.8 seconds)
  because fast state extraction took less than one millisecond.
- Strategy latency remains highly variable and is now the clearest foreground
  optimization target.
- Deep-state and rolling-summary structured-output reliability remains a
  separate 0.8B prompt/output-budget problem.

## Phase 2: persisted deep state for N+1 (2026-08-05)

This section supersedes the Phase 1 shadow-only persistence statements above.
The Phase 1 implementation history and measurements are retained for reference.

### Implemented behavior

- The current database is still shared with v2. Migration
  `0003_deep_state_results` is additive and introduces only the new
  `deep_state_results` table, constraints, and lookup indexes.
- v2 code and processes were not modified or restarted.
- Deep inference for turn N still starts in the background before the foreground
  reply completes. It is not moved back into the serialized fast path.
- The deep result waits on a foreground transaction commit gate before it may be
  persisted. A committed turn persists `ready` or `failed`; a rolled-back turn
  discards the background result instead of creating an orphan record.
- A successful result is persisted immediately after both inference completion
  and source-turn commit. Persistence uses an independent SQLAlchemy session.
- Turn N+1 reads the newest `ready` candidate whose source message sequence is
  older than the current user message. The read does not wait for unfinished
  deep inference.
- If no candidate is ready, N+1 continues with fast state only.
- The merge order is: previous official state, latest completed deep delta, then
  current-turn fast delta. Therefore explicit current-turn information wins
  over older deep analysis.
- The reducer's intermediate deep version is normalized back to the previous
  official version before applying fast state, so one foreground turn still
  creates exactly one new official state version.
- The consumed candidate is marked `applied` and older ready candidates are
  marked `expired` in the same foreground transaction as the new state version.
- Candidate selection uses row locking with `SKIP LOCKED`; persistence is
  idempotent on source message plus pipeline version.
- Background failure categories are persisted without user content and never
  fail the foreground response.

### Current execution order

Foreground turn N:
feedback -> (risk || fast state) -> read latest ready deep result -> apply deep
result -> apply current fast result -> save one state version -> memory ->
strategy -> enqueue current deep computation -> response -> output guard ->
commit -> return

Background for turn N:
deep inference starts after enqueue and runs concurrently with reply/output
work -> wait for source transaction outcome -> persist `ready` or `failed` in an
independent transaction -> allow same-session post-turn summary to continue

If N+1 arrives while N deep inference is unfinished, N+1 performs only the
non-blocking database read and proceeds immediately. The unfinished N result may
be consumed by a later turn after it becomes ready.

### Added or changed files

- `schemas/deep_state.py`: typed persisted candidate and lifecycle status.
- `storage/models/deep_state.py`: ORM table and lifecycle constants.
- `storage/repositories/deep_state_repository.py`: in-memory and PostgreSQL
  repository implementations.
- `storage/migrations/versions/0003_deep_state_results.py`: additive migration.
- `orchestrator/deep_state_pipeline.py`: commit gate plus ready/failed callbacks.
- `orchestrator/turn_orchestrator.py`: N+1 read, merge, apply, and expire flow.
- `runtime/sqlalchemy_orchestrator.py`: transaction-local queue wrapper that
  releases or cancels background persistence after commit/rollback.
- `runtime/factory.py`: independent persistence transactions for deep results.
- `tests/unit/test_deep_state_n_plus_one.py`: verifies deep context reaches
  strategy, current fast goal wins, candidate becomes applied, and one official
  version is created.

### Migration and validation

- Pre-change code snapshot:
  `/data/agent/psych-agent/backups/psych-agent_v3_pre_deep_nplus1_20260805.tar.gz`
  (SHA-256 `ce1eb860248eb4d7513a5d5580c5531f479df01f5b2f93e8f02a9bda8ab19246`).
- Pre-migration database dump:
  `/data/agent/psych-agent/backups/psych_agent_pre_deep_nplus1_20260805.dump`
  (SHA-256 `b999221f892ef4a4ad59fd0dd5cf97f8c293ba5d39127aabe36ba7bf14f6f0e1`).
- Alembic upgraded from `0002_user_scoped_sessions` to
  `0003_deep_state_results`.
- The post-migration database check reported the expected head, table,
  constraints, and schema status as OK.
- 54 focused regression tests passed after deployment.
- v3 on port 8011 and the protected existing service on port 8000 both returned
  HTTP 200 with status `ok` after the v3 restart.
- A live synthetic N+1 test verified `ready -> applied`, preservation of a deep
  strategy preference, current fast-goal precedence, and one state-version
  increment (2 -> 3).
- Live natural 0.8B deep attempts also verified non-blocking failure persistence:
  one output was truncated and later outputs failed structured parsing, while
  all foreground turns returned successfully. This remains a separate model
  output-budget/prompt reliability issue; it does not invalidate the storage and
  N+1 consumption path.

### Activation and rollback after Phase 2

`ASYNC_STATE_PIPELINE_ENABLED=true` enables fast foreground state plus persisted
background deep state for N+1. Setting it to false returns v3 to the legacy
foreground tracker; the additive table may remain in the shared database and is
ignored by v2. A schema downgrade is not required for feature rollback and
should only be performed during a separate maintenance decision.

## Phase 3: reliable long-term memory reads and recall (2026-08-06)

### Implemented behavior

- The database remains shared with v2. This phase changes v3 application code only;
  it adds no migration and does not change v2 code or processes.
- Every normal v3 turn reads the newest active long-term memories before strategy
  planning. The safety route still takes precedence and does not wait on memory.
- Active-memory retrieval now applies the result limit in SQL instead of loading
  all active rows and slicing them in Python.
- Pending-confirmation content is never injected as an active fact. Its count is
  queried only for an explicit memory question, so ordinary turns do not pay for
  the extra query.
- Memory questions are classified deterministically as existence, list, or
  specific recall. They use a local summarization strategy and a grounded local
  response instead of calling the normal strategy and response models.
- Specific recall ranks active memories against the current user message. Generic
  questions return a short list of the newest active memories.
- A query with only pending candidates says that records are awaiting
  confirmation; a query with no records says that no usable record is available.
  The two states are intentionally not conflated.
- Normal non-memory turns still receive the current raw user message plus the
  classified memory intent in strategy input, and continue through the regular
  model pipeline.
- The output guard now recognizes IDs from every active memory type, including
  goals and interaction preferences. It blocks unknown IDs, false categorical
  denial, and generic recall answers that ignore available active records.
- After local memory, ID, and safety rules pass, deterministic memory-query
  responses skip the semantic guard model. This avoids model-side false blocking
  while retaining the normal risk analysis and local safety checks.
- The complete ordered active-memory list is retained only inside v3 for local
  grounding and ID validation. It is excluded from model serialization to avoid
  duplicating the categorized memory payload.

### Current execution paths

Normal turn:
risk/fast state -> deep N+1 merge -> read active memory -> model strategy ->
background deep enqueue -> model response -> output guard -> commit -> return

Explicit memory query:
risk/fast state -> deep N+1 merge -> read active memory and pending count ->
local summarization strategy -> background deep enqueue -> grounded local reply
-> local output guard -> commit -> return

### Added or changed files

- `schemas/memory.py`: memory-query intent and typed memories for every category.
- `schemas/context.py` and `schemas/strategy.py`: current message and query intent.
- `storage/repositories/memory_repository.py`: limited active read and pending
  count.
- `services/memory_retriever.py`: per-turn retrieval and complete ID ownership.
- `services/memory_query.py`: intent detection, ranking, grounded drafts, and
  false-denial detection.
- `services/context_builder.py`: forwards current message and intent.
- `orchestrator/turn_orchestrator.py`: selects normal versus fast recall paths.
- `agents/strategy_planner.py`, `agents/response_agent.py`, and
  `agents/output_guard.py`: prompt grounding and memory-reference enforcement.
- `tests/unit/test_memory_query.py`, `tests/unit/test_memory_query_turn.py`, and
  `tests/unit/test_memory_retriever.py`: query, orchestration, ID, and SQL-limit
  coverage.

### Validation and measured latency

- Pre-change v3 snapshot:
  `/data/agent/psych-agent/backups/psych-agent_v3_pre_memory_reliability_20260806.tar.gz`
  (SHA-256 `872bb15fd0a0ddfcad3442e4bebd4e6cb760a92b58c0bf67428d4baee659d015`).
- 341 unit tests passed after excluding five existing SQLite-dependent files.
  In the full unit run, 366 tests passed; 8 failures and 26 setup errors all came
  from the server environment lacking the existing `aiosqlite` test dependency.
- Live generic recall returned in 2.37 s with an 18.55 ms memory read. Live
  specific recall returned in 2.16 s with a 2.10 ms memory read. Their local
  strategy/response/guard stages were each below 0.20 ms after the fix.
- A live ordinary turn read memory in 1.21 ms and then followed the normal model
  path; its total response time was 11.86 s. Therefore the memory read itself is
  negligible relative to model inference.
- v3 on port 8011 and protected v2 on port 8000 returned HTTP 200 after restart.

### Known data-quality boundary

The exact `华晨宇烟台音乐节` memory in the synthetic account is still pending
confirmation, while two older active preference records are vague. v3 now recalls
only active records and truthfully reports the pending count; it does not claim
that the user attended an event. Improving memory extraction/confirmation quality
and persisting per-turn referenced-memory audit data are separate follow-up work.

## Phase 4: unified per-turn long-term-memory flow (2026-08-06)

This section supersedes the Phase 3 memory-query fast-path statements. Phase 3
is retained as implementation history, but memory questions no longer bypass the
normal strategy, response, or semantic output-guard agents.

### Final behavior

- Every non-safety turn reads the newest active memories before strategy planning.
  Safety routing remains earlier so urgent safety responses do not wait on memory.
- Every non-safety turn also reads a bounded page of pending-confirmation records.
  PostgreSQL returns the page and total pending count in one query by using a
  window count.
- Pending candidates are locally ranked against the exact current user message.
  Generic memory existence/list questions receive the newest candidates; ordinary
  and specific messages receive only candidates with topic overlap.
- `pending_confirmation_memories` is a separate serialized field. It is never
  merged into active categories and must be described as unconfirmed.
- `memory_query_intent` remains deterministic metadata for strategy grounding; it
  does not select a different orchestration route.
- Every memory question and ordinary message now calls the same model stages:
  `agent.strategy`, `agent.response`, then the complete semantic
  `agent.output_guard` after local rules.
- Strategy and response have standard deterministic fallbacks only when their
  model call fails; this is the normal agent failure path, not a memory-query
  route.
- The response agent may reference IDs from owned active or retrieved pending
  records. The guard blocks unknown IDs, false denial of relevant records, and
  any referenced pending record that is not clearly labeled unconfirmed.

### Final execution path

Normal non-safety turn:
risk/fast state -> deep N+1 merge -> SQL active read -> SQL pending page plus
count -> local pending relevance filter -> model strategy -> background deep
enqueue -> model response -> local plus semantic output guard -> commit -> return

The same path applies to `你还记得我吗`, specific recall questions, and ordinary
messages. Only the retrieved content and strategy instruction differ.

### Validation

- Static compilation passed for all changed schemas, repositories, services,
  agents, orchestration, and tests.
- 17 focused memory tests passed, including unified turn routing, per-turn pending
  reads, relevance filtering, serialization, ID ownership, and pending-label
  enforcement.
- 342 unit tests passed after excluding the five existing files that require the
  unavailable `aiosqlite` test dependency.
- Live `你还记得我吗` returned a natural model-generated answer with HTTP 200.
  Its memory read took 16.50 ms and its trace contained `agent.strategy`,
  `agent.response`, and `agent.output_guard`; total server time was 14.34 s.
- A live ordinary music-festival message also returned HTTP 200 through the same
  three model stages. Its memory read took 2.68 ms; total server time was 20.16 s.
  The additional time was model inference, not database retrieval.
- Direct read-only production-repository verification for the synthetic account
  returned two active and two total pending records on every checked turn. The
  generic memory question selected both pending candidates, the ordinary
  music-festival message selected one related candidate, and an unrelated
  ordinary message selected none.
- No database migration was added. v3 was restarted on port 8011 only; protected
  v2 on port 8000 was not modified or restarted. Both health endpoints returned
  HTTP 200 after deployment.

### Memory-guard correction after browser testing

A live browser test exposed that a safe recall draft could be converted to the
generic safety-failure message by local memory metadata rules. The trace showed a
successful 2.10 ms memory read and successful strategy/response calls, followed
by a 0.134 ms guard decision with no semantic-model call.

The correction keeps the unified pipeline and changes only guard failure
handling:

- Missing `referenced_memory_ids` metadata no longer blocks an otherwise safe
  draft; it continues to the semantic output guard.
- False denial or confirmed wording for pending content is rewritten with a
  grounded memory-specific response instead of the unrelated generic safety
  message.
- Unknown memory IDs, actual unsafe content, and safety-route violations remain
  blocked as before.
- 18 focused memory tests and 343 effective unit tests passed.
- A new live session answered `你还记得我吗` with both active records and the
  two-record pending count. Memory retrieval took 12.39 ms, the local controlled
  rewrite took 0.221 ms, total server time was 15.51 s, and both v2/v3 health
  checks remained HTTP 200.
