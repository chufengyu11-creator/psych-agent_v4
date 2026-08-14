# 安装与运行小教程

这份文档用于本地或服务器快速跑通当前基座。当前推荐先跑 smoke 脚本确认数据库链路，再考虑接真实本地大模型。

## 1. 准备 Python 环境

项目以 Python 3.11 为开发、类型检查和部署目标，推荐使用最新的 Python 3.11.x。

如果用 conda：

```bash
conda create -n psych-agent python=3.11 -y
conda activate psych-agent
```

确认版本：

```bash
python --version
```

输出应为 `Python 3.11.x`。如果不使用 conda，也可以创建独立虚拟环境：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
```

Windows PowerShell 使用：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

## 2. 安装项目依赖

进入项目根目录后执行 editable 安装：

```bash
python -m pip install -e ".[dev]"
```

如果服务器只运行服务、不跑测试，可以只装运行依赖：

```bash
python -m pip install -e .
```

不过目前 smoke 脚本使用 SQLite 异步驱动 `aiosqlite`，它在 dev 依赖里，所以服务器要跑 smoke 时也建议使用 `.[dev]`。

## 3. 配置环境变量

复制示例配置：

```bash
cp .env.example .env
```

当前 `.env.example` 里的关键项：

```bash
APP_ENV=development
APP_HOST=0.0.0.0
APP_PORT=8000
DATABASE_URL=postgresql+asyncpg://psych_agent:replace-me@127.0.0.1:5432/psych_agent
TEST_DATABASE_URL=postgresql+asyncpg://psych_agent:replace-me@127.0.0.1:5432/psych_agent_test
REDIS_URL=redis://127.0.0.1:6379/0
REDIS_REQUIRED=false
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=replace-me
MAIN_MODEL_NAME=deepseek-v4-flash
STRUCTURED_MODEL_NAME=deepseek-v4-flash
SAFETY_MODEL_NAME=deepseek-v4-flash
LLM_TIMEOUT_SECONDS=60
LLM_THINKING_MODE=disabled
```

当前开发环境可以通过 DeepSeek 的 OpenAI-compatible API 验证模型链路。API Key 只应写入本地 `.env` 或服务器密钥管理系统；不要写入源码、测试、日志或文档，`.env` 也不得进入 Git。建议使用 `/models` 返回的当前模型 ID，不要继续依赖即将退役的旧模型别名。环境变量中的模型名必须与 endpoint 实际 served model name 完全一致。

`LLM_THINKING_MODE` 支持 `omit`、`enabled`、`disabled`。默认推荐 `omit` 以兼容普通 OpenAI-compatible endpoint；DeepSeek 开发验证可显式设为 `disabled`。客户端只使用最终 `content`，不会返回或保存 `reasoning_content`。

两条业务 smoke 默认创建独立的临时 SQLite 数据库，也支持显式选择预先迁移的 PostgreSQL。真实 PostgreSQL 必须先执行 Alembic 迁移；Docker、本机 PostgreSQL、密码 URL 编码、连接检查和显式集成测试见 [PostgreSQL 本地开发文档](postgres_development.md)。

## 4. 初始化并检查 PostgreSQL

配置本地 `.env` 后执行：

```bash
alembic upgrade head
alembic current
python scripts/check_database_connection.py
python scripts/check_database_connection.py --json
```

配置独立的 `TEST_DATABASE_URL` 后，显式运行真实 PostgreSQL 测试：

```bash
pytest tests/integration/postgres -m postgres
```

默认 `pytest` 不要求 PostgreSQL；未配置测试库时这些用例会明确 skip。正式 PostgreSQL 初始化不得使用 `Base.metadata.create_all()`。

## 5. 检查 OpenAI-compatible 模型端点

完成本地 `.env` 配置后执行：

```bash
python scripts/check_llm_endpoint.py
python scripts/check_llm_endpoint.py --json
python scripts/smoke_real_structured_agents.py
```

第一条脚本依次检查配置、`GET /models`、普通 chat completion 和 JSON Output；第二条真实 smoke 只调用一次 RiskAgent、StateTracker 和 StrategyPlanner，不调用 ResponseAgent、OutputGuard 或数据库。如果本地 endpoint 不实现 `/models`，可以执行：

```bash
python scripts/check_llm_endpoint.py --skip-model-list
```

真实调用会产生 API 费用并向配置的 endpoint 发送数据。检查脚本只使用内置人工消息；不要将真实用户心理咨询数据用于 smoke。

生产服务器切换到本地 Qwen/vLLM 时只需修改环境变量，例如：

```bash
LLM_BASE_URL=http://127.0.0.1:8001/v1
LLM_API_KEY=local-dev-key
MAIN_MODEL_NAME=qwen-7b
STRUCTURED_MODEL_NAME=qwen-7b
SAFETY_MODEL_NAME=qwen-7b
LLM_TIMEOUT_SECONDS=60
LLM_THINKING_MODE=omit
```

这里的 `qwen-7b` 只是 served model name 示例，必须替换为 vLLM 启动时实际暴露的模型 ID。切换 provider 不需要修改 Agent、StructuredLLMClient、Orchestrator 或数据库代码。

## 6. 跑基础检查

```bash
python -m pip check
python -c "import app, runtime, storage, llm, agents, orchestrator"
ruff check .
mypy .
pytest
```

如果只想先验证最近的第二批链路：

```bash
pytest tests/unit/test_session_finalizer.py tests/integration/test_sqlalchemy_memory_repository.py
```

## 7. 跑 DB smoke：多轮对话到 rolling summary

```bash
python scripts/smoke_model_assisted_database_corpus.py --database sqlite --ascii
```

不写 `--database` 时仍默认 SQLite，因此旧命令保持兼容。输出应包含：

```text
success=True
database_backend=sqlite
messages_rows=6
intervention_events_rows=3
rolling_summary_versions_rows=1
structured_llm_calls=9
```

这说明已经跑通：

```text
多轮对话 -> messages -> intervention_events -> rolling_summary_versions
```

## 8. 跑 DB smoke：session close 到长期记忆

```bash
python scripts/smoke_session_close_memory.py --database sqlite --ascii
```

输出应包含：

```text
candidate_memories=1
memory_writes=1
session_status=closed
long_term_memories_rows=1
source_message_ids_non_empty=True
```

这说明已经跑通：

```text
多轮对话 -> rolling_summary_versions -> session close -> long_term_memories
```

需要机器可读结果时，对任一命令使用 `--json`。两个脚本都只输出本轮安全标识和统计，不输出完整对话、长期记忆内容、数据库密码或完整 URL。

Linux 上使用独立 staging/acceptance PostgreSQL 时，必须先运行 `alembic upgrade head`，然后把参数改为 `--database postgres`。PostgreSQL smoke 不创建表、不执行迁移、不回退 SQLite，默认保留本轮数据；只有显式传入 `--cleanup` 才删除本轮唯一 ID 对应的数据。真实 PostgreSQL 业务验收在当前 Windows 阶段仍为 deferred。

## 9. 可选：启动 API 服务

启动 FastAPI：

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

单轮对话请求：

```bash
curl -X POST http://127.0.0.1:8000/chat/turn \
  -H "Content-Type: application/json" \
  -d '{"user_id":"user_demo","session_id":"session_demo","message":"我最近工作压力很大，不知道该怎么跟领导沟通。"}'
```

## 10. 服务器上建议先验证什么

建议按这个顺序：

```bash
python --version
pip check
ruff check .
mypy .
pytest
python scripts/smoke_model_assisted_database_corpus.py --database sqlite --ascii
python scripts/smoke_session_close_memory.py --database sqlite --ascii
```

## 11. Redis infrastructure and health endpoints

Redis is optional infrastructure in the current stage; it is not yet a task
queue and no worker is running. Configure it only in a local `.env` or server
secret manager:

```bash
REDIS_URL=redis://127.0.0.1:6379/0
REDIS_REQUIRED=false
```

Run the safe read-only check with:

```bash
python scripts/check_redis_connection.py
python scripts/check_redis_connection.py --json
```

FastAPI exposes process liveness at `/health/live` and dependency readiness at
`/health/ready`. A 503 readiness response means at least one required component
is unavailable; it does not mean process liveness is down. Health checks never
write database rows or Redis keys, and the LLM check sends no user or
psychological-support data. See `docs/redis_and_health.md`. Real Redis
acceptance remains deferred to Linux.

如果这些都通过，再按 PostgreSQL 文档在独立数据库上执行迁移、连接检查、显式数据库测试和 `--database postgres` 业务 smoke。不要把真实用户心理数据用于 smoke。Redis 和生产部署仍属于后续步骤。
