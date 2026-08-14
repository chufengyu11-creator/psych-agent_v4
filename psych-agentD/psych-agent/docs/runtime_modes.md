# Application Runtime Modes

`APP_RUNTIME_MODE` selects one explicit runtime. Runtime selection never depends on
the LLM provider hostname and never falls back to another mode after startup.

| Mode | Persistence | Risk/State/Strategy | Intended use |
| --- | --- | --- | --- |
| `in_memory` | In-memory repositories | Existing fake agents | Unit tests and quick local checks |
| `sqlalchemy_fake` | SQLAlchemy repositories selected by `DATABASE_URL` | Existing fake agents | Deterministic database-flow tests |
| `sqlalchemy_model` | SQLAlchemy repositories selected by `DATABASE_URL` | OpenAI-compatible model-backed agents | Explicit model-assisted runtime |

Production startup rejects the default `in_memory` mode. In
`sqlalchemy_model`, missing database or LLM configuration fails startup; the
runtime does not switch to `sqlalchemy_fake`, an in-memory repository, or a
scripted model client.

## Resource lifecycle

FastAPI startup creates one `ApplicationRuntime` and stores it in
`app.state.runtime`. One async Engine, session factory, and OpenAI-compatible
HTTP client are shared by the application process. Every turn still creates a
new `AsyncSession` and outer transaction through `transactional_session`.
Shutdown closes the HTTP connection pool and disposes the Engine; repeated
shutdown calls are safe.

In `sqlalchemy_model`, RiskAgent, StateTracker, FeedbackEvaluator,
StrategyPlanner, RollingSummarizer, SessionFinalizer, and MemoryCurator use the
shared structured-model client. FeedbackEvaluator retains its internal
deterministic fallback for model, schema, or semantic-validation failures.
ResponseAgent and OutputGuard remain their existing fake implementations. The
task queue remains `NoopTaskQueue`.
Redis can be owned as an optional infrastructure client for readiness, but no
Redis queue or worker is connected. See `redis_and_health.md`.

## Local DeepSeek plus temporary SQLite smoke

Keep the real API key only in the local `.env` or a server secret manager. The
manual smoke uses two artificial, non-sensitive turns, creates only its own
temporary SQLite file, and makes six structured Agent calls when all model
outputs validate:

```bash
APP_ENV=development
APP_RUNTIME_MODE=sqlalchemy_model
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=replace-me
STRUCTURED_MODEL_NAME=deepseek-v4-flash
LLM_THINKING_MODE=disabled
```

```bash
python scripts/smoke_model_sqlalchemy_runtime.py --ascii
python scripts/smoke_model_sqlalchemy_runtime.py --json
```

The smoke overrides `DATABASE_URL` only in memory with a unique SQLite URL. It
does not alter `.env`, connect to PostgreSQL, or run Alembic. Its temporary file
is deleted by default; `--keep-db` retains that one file for manual inspection.
Real calls incur API charges. Never use real psychological-support messages in
this smoke.

SQLite `Base.metadata.create_all()` is permitted only for such isolated local
temporary tests. It must not initialize formal PostgreSQL environments.

## Linux PostgreSQL with DeepSeek or Qwen/vLLM

The same runtime code is provider- and database-agnostic. A later Linux
environment can select PostgreSQL and a served Qwen model using only variables:

```bash
APP_ENV=production
APP_RUNTIME_MODE=sqlalchemy_model
DATABASE_URL=postgresql+asyncpg://user:password@database-host/database-name
REDIS_URL=redis://127.0.0.1:6379/0
REDIS_REQUIRED=true
LLM_BASE_URL=http://127.0.0.1:8001/v1
LLM_API_KEY=local-service-key
STRUCTURED_MODEL_NAME=<served-model-name>
LLM_THINKING_MODE=omit
```

Run `alembic upgrade head` before starting a PostgreSQL runtime. The factory
does not run migrations or `create_all()`. Real PostgreSQL acceptance and B-5B
sequence/version concurrency controls remain deferred.

Default pytest uses only fake clients or `httpx.MockTransport`; it does not
contact DeepSeek, PostgreSQL, or Redis.

## Long-term memory authorization

SQLAlchemy users default to `memory_enabled=false`. Session close reads the
current value from `users.memory_enabled` inside the same transaction used for
summary, memory policy, writes, and closing the session. Both
`sqlalchemy_fake` and `sqlalchemy_model` use this authorization gate.

When memory is disabled, finalization and summary still complete and candidates
still pass through MemoryPolicy, which rejects persistence with
`memory_disabled`. The project does not yet expose a public API for changing
this setting; a later user-settings API or a controlled database process must
manage it.
