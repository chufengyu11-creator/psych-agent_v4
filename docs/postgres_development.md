# PostgreSQL 本地开发与显式集成测试

本页只描述 PostgreSQL、asyncpg 和 Alembic。推荐使用 Python 3.11 独立环境；不要把示例密码用于生产，也不要将 PostgreSQL 端口暴露到公网。

## 配置约定

开发或生产运行读取 `DATABASE_URL`，显式 PostgreSQL 集成测试只读取 `TEST_DATABASE_URL`：

```dotenv
DATABASE_URL=postgresql+asyncpg://psych_agent:replace-me@127.0.0.1:5432/psych_agent
TEST_DATABASE_URL=postgresql+asyncpg://psych_agent:replace-me@127.0.0.1:5432/psych_agent_test
```

测试数据库名必须包含 `test`，否则集成测试会拒绝运行。真实密码只放在本地 `.env` 或服务器密钥管理中；`.env` 不进入 Git。

如果密码包含 `@`、`:`、`/`、`#`、`%` 等 URL 保留字符，必须先做百分号编码。示例：

```powershell
python -c "from urllib.parse import quote; print(quote('replace-me', safe=''))"
```

```bash
python -c "from urllib.parse import quote; print(quote('replace-me', safe=''))"
```

不要把包含真实密码的完整 URL 打印到终端、日志或实施报告。

## 方案 A：Docker Compose（本地开发）

仓库的 `compose.postgres.yml` 只启动 PostgreSQL，端口仅绑定到 `127.0.0.1`，并使用 named volume 和 healthcheck。它不会启动 Redis，也不会由应用自动创建数据库。

先在本地 `.env` 设置 Compose 使用的开发值：

```dotenv
POSTGRES_USER=psych_agent
POSTGRES_PASSWORD=replace-me
POSTGRES_DB=psych_agent
POSTGRES_PORT=5432
```

Windows PowerShell：

```powershell
docker compose -f compose.postgres.yml up -d
docker compose -f compose.postgres.yml ps
docker compose -f compose.postgres.yml exec postgres createdb -U psych_agent -O psych_agent psych_agent_test
```

Linux/bash：

```bash
docker compose -f compose.postgres.yml up -d
docker compose -f compose.postgres.yml ps
docker compose -f compose.postgres.yml exec postgres createdb -U psych_agent -O psych_agent psych_agent_test
```

如果测试数据库已经存在，`createdb` 会明确报错，不需要删除或重建现有数据库。Compose 中的占位密码仅供本机开发模板使用；生产必须改用强密码和密钥管理。

## 方案 B：本机原生 PostgreSQL

以下 SQL 需要由 PostgreSQL 管理员执行。请将示例密码替换为本地强密码：

```sql
CREATE ROLE psych_agent LOGIN PASSWORD 'replace-me';
CREATE DATABASE psych_agent OWNER psych_agent;
CREATE DATABASE psych_agent_test OWNER psych_agent;
GRANT ALL PRIVILEGES ON DATABASE psych_agent TO psych_agent;
GRANT ALL PRIVILEGES ON DATABASE psych_agent_test TO psych_agent;
```

Windows 可以使用 PostgreSQL 安装包附带的 SQL Shell (`psql`)；Linux 通常使用：

```bash
sudo -u postgres psql
```

开发库和测试库必须分离。不要让 `TEST_DATABASE_URL` 指向生产库或不含 `test` 的数据库。

## Alembic 初始化和检查

正式 PostgreSQL 表结构只能由 Alembic 创建。服务启动前先执行：

```powershell
alembic upgrade head
alembic current
alembic history
python scripts/check_database_connection.py
python scripts/check_database_connection.py --json
```

Linux/bash 使用相同命令：

```bash
alembic upgrade head
alembic current
alembic history
python scripts/check_database_connection.py
python scripts/check_database_connection.py --json
```

只检查连接而暂不检查迁移结构：

```bash
python scripts/check_database_connection.py --skip-schema-check
```

检查测试数据库：

```bash
python scripts/check_database_connection.py --url-env TEST_DATABASE_URL
```

连接检查只输出 driver、host、port、数据库名、PostgreSQL 版本摘要、Alembic revision、七张表和关键对象状态，不输出密码、完整 URL 或业务数据。

不要使用 `Base.metadata.create_all()` 代替 Alembic。当前项目不承诺生产环境 downgrade；如需回退，应先备份并单独评审迁移计划。数据库备份策略留到最终部署阶段展开。

## 可配置数据库业务 smoke

没有 PostgreSQL 服务时，两个业务 smoke 默认使用各自独立的临时 SQLite 文件；也可以显式指定 SQLite：

```bash
python scripts/smoke_model_assisted_database_corpus.py --database sqlite --ascii
python scripts/smoke_session_close_memory.py --database sqlite --ascii
```

部署到 Linux 的独立 staging 或 acceptance PostgreSQL 数据库后，先用 Alembic 建表，再运行相同业务流程：

```bash
alembic upgrade head
python scripts/smoke_model_assisted_database_corpus.py --database postgres --ascii
python scripts/smoke_session_close_memory.py --database postgres --ascii
```

PostgreSQL 模式只读取 `DATABASE_URL`，且要求 `postgresql+asyncpg`；它不会读取 `TEST_DATABASE_URL`、不会调用 `create_all()`/`drop_all()`、不会执行迁移，也不会回退到 SQLite。若未迁移，会提示先运行 Alembic。

每次运行使用唯一的虚拟 user/session ID，统计只覆盖本轮数据。PostgreSQL 默认保留这些数据供人工验收；只有显式传入 `--cleanup` 才按外键安全顺序删除本轮数据。不要对生产数据库随意运行 smoke，推荐使用独立 staging/acceptance 数据库，且只能使用脚本构造的虚拟数据。`--json` 可输出同一份不含密码、完整 URL 或对话内容的机器可读结果。

当前 Windows 阶段只完成了 SQLite 业务验收和 PostgreSQL 代码路径的离线安全测试；真实 PostgreSQL 业务 smoke 仍为 deferred，需在 Linux 环境补验。

Memory 所有权、脱敏写入、来源追踪、User/Session 幂等和事务约定见 [Repository 数据安全约定](repository_data_safety.md)。其中 PostgreSQL savepoint、SQLSTATE 和并发行为同样仍需 Linux 验收。

## 显式 PostgreSQL 测试

未配置 `TEST_DATABASE_URL` 时，PostgreSQL 测试会明确 skip，默认 pytest 不依赖数据库服务。配置独立测试库后运行：

```powershell
pytest tests/integration/postgres -m postgres
```

```bash
pytest tests/integration/postgres -m postgres
```

测试启动时会对 `TEST_DATABASE_URL` 执行 `alembic upgrade head`，不会调用 `create_all()`，不会 drop 数据库或清空全表，只删除本次测试使用唯一 ID 创建的数据。测试覆盖连接、revision、七张表、关键约束、Repository 往返、JSONB、时区时间戳、commit、rollback、外键、唯一约束和部分唯一索引。

## 停止本地容器

```bash
docker compose -f compose.postgres.yml down
```

该命令默认保留 named volume。不要随意加 `--volumes`，除非明确确认要删除全部本地开发数据。
