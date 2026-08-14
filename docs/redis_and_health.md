# Redis Infrastructure and Health Checks

Redis is currently an application-owned infrastructure resource only. No
Redis-backed TaskQueue, worker, publish operation, consumer loop, Celery, RQ,
or Dramatiq integration exists in this stage. `NoopTaskQueue` remains active.

## Configuration

```bash
REDIS_URL=redis://127.0.0.1:6379/0
REDIS_REQUIRED=false
REDIS_CONNECT_TIMEOUT_SECONDS=2
REDIS_SOCKET_TIMEOUT_SECONDS=2
HEALTH_CHECK_TIMEOUT_SECONDS=3
```

`REDIS_URL` may use `redis://` or `rediss://`. It is optional by default and is
stored as a secret setting so credentials are excluded from repr output. Do not
commit a password to Git. Production Redis should use authentication and an
internal network or Unix-socket deployment; do not expose Redis to the public
Internet. `rediss://` is available for TLS environments.

`REDIS_REQUIRED` is explicit and does not depend on a provider hostname:

- `false` and no URL: no client is created; readiness reports
  `not_configured` and remains ready.
- `false` with a URL: one lazy client pool is created and readiness reports its
  PING result, but a Redis failure does not block overall readiness.
- `true` with no URL: Runtime construction fails.
- `true` in development/test: Runtime may start, but failed PING makes
  `/health/ready` return 503.
- `true` in production: startup performs a bounded PING and fails if Redis is
  unavailable; readiness continues checking the same shared client.

Creating the redis-py object is lazy and does not connect until PING or a later
command. One `ApplicationRuntime` owns one pool. Shutdown closes Redis first,
then the LLM HTTP client, then the SQLAlchemy Engine. A partial startup closes
every resource already created.

Future D workers must receive or reuse `ApplicationRuntime.redis_client`; they
must not create a second Redis pool in worker modules. Queue commands and worker
business behavior remain separate work.

## Health endpoints

The API preserves `GET /health` and adds:

- `GET /health/live`: process liveness only; it never accesses database,
  Redis, or LLM.
- `GET /health/ready`: fresh read-only component checks. It returns HTTP 200
  with `status=ready`, or HTTP 503 when a required component fails.

Required components are selected by Runtime configuration:

| Runtime mode | Database | LLM | Redis |
| --- | --- | --- | --- |
| `in_memory` | not required | not required | `REDIS_REQUIRED` |
| `sqlalchemy_fake` | required | not required | `REDIS_REQUIRED` |
| `sqlalchemy_model` | required | required | `REDIS_REQUIRED` |

Checks are bounded by `HEALTH_CHECK_TIMEOUT_SECONDS` and are not cached:

- Database: one short connection executes `SELECT 1`. Non-SQLite databases
  also read one `alembic_version` value. SQLite intentionally skips the
  Alembic check so isolated local `create_all()` smoke databases can be ready.
- Redis: the Runtime-owned client executes only `PING`; it does not write a key
  or run `INFO`.
- LLM: the Runtime-owned OpenAI-compatible client performs one non-retried
  `GET /models` and verifies only the configured structured model name. It does
  not make a completion call or invoke RiskAgent, StateTracker, or
  StrategyPlanner.

Responses contain required/status/latency/error-code fields only. They never
contain passwords, API keys, complete URLs, model lists, prompts, raw driver
errors, or tracebacks.

## Standalone Redis check

```bash
python scripts/check_redis_connection.py
python scripts/check_redis_connection.py --json
python scripts/check_redis_connection.py --timeout 2
```

The script reuses Settings and the formal Redis factory. It prints only safe
endpoint metadata and one PING result. When Redis is absent or unavailable it
exits non-zero without a traceback. Default pytest uses injected clients and
does not contact Redis.

Real Redis and PostgreSQL acceptance remain deferred to the Linux environment.
Health checks do not initialize schemas or write database/Redis data.
