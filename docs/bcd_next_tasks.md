# B/C/D 后续分工说明

当前主线已经跑通两个 fake/脚本闭环：

```text
多轮对话 -> messages -> intervention_events -> rolling_summary_versions
多轮对话 -> session close -> long_term_memories
```

接下来目标是：保持现有 `schemas/`、数据库表结构、orchestrator 主流程尽量稳定，逐步把 fake 组件替换成真实模型和真实服务器环境。

## B：Qwen 7B 接入 + 数据库/服务器环境

### 简短目标

让项目能在服务器上跑起来，并通过本地 Qwen 7B endpoint 调用模型；同时保证数据库链路稳定。

### 主要任务

1. 准备服务器 Python 环境。
2. 安装项目依赖。
3. 配置 `.env`。
4. 启动或连接 Qwen 7B 的 OpenAI-compatible endpoint。
5. 准备 Postgres / Redis。
6. 跑通基础检查和 smoke。
7. 后续把当前 SQLite smoke 迁移到服务器 Postgres 测试。

### 重点文件

B 主要看这些文件：

```text
pyproject.toml
.env.example
app/config.py
llm/client.py
llm/structured_client.py
storage/database.py
storage/models/
storage/repositories/
storage/migrations/
scripts/smoke_model_assisted_database_corpus.py
scripts/smoke_session_close_memory.py
docs/install_and_run_quickstart.md
```

### 环境变量

需要重点配置：

```bash
APP_ENV=development
APP_HOST=0.0.0.0
APP_PORT=8000
DATABASE_URL=postgresql+asyncpg://psych_agent:psych_agent@localhost:5432/psych_agent
REDIS_URL=redis://localhost:6379/0
LLM_BASE_URL=http://localhost:8001/v1
LLM_API_KEY=replace-me
MAIN_MODEL_NAME=qwen-7b
STRUCTURED_MODEL_NAME=qwen-7b
SAFETY_MODEL_NAME=qwen-7b
```

如果 Qwen endpoint 不需要 key，可以先放一个占位值：

```bash
LLM_API_KEY=local-dev-key
```

### B 需要保证的数据流

```text
.env
-> Settings
-> LLMClient / StructuredLLMClient
-> RiskAgent / StateTracker / StrategyPlanner
```

数据库链路：

```text
Settings.DATABASE_URL
-> create_engine()
-> transactional_session()
-> SqlAlchemy repositories
-> Postgres tables
```

### B 需要验证的表

```text
users
sessions
messages
session_state_versions
intervention_events
rolling_summary_versions
long_term_memories
```

### 验收命令

```bash
python --version
pip check
ruff check .
mypy .
pytest
python scripts/smoke_model_assisted_database_corpus.py --ascii
python scripts/smoke_session_close_memory.py --ascii
```

### 完成标准

B 完成后应该能说明：

```text
1. 服务器 Python 环境可以安装项目。
2. Qwen 7B endpoint 可以被项目访问。
3. ruff/mypy/pytest 全过。
4. 两个 smoke 全过。
5. smoke 输出里 rolling_summary_versions rows=1。
6. smoke 输出里 long_term_memories rows=1。
```

## C：真实 structured agents

### 简短目标

把当前 fake/scripted 的结构化模型能力，逐步替换成真实 Qwen 7B 输出，并保证 JSON 稳定。

### C 优先负责的 agent

按这个顺序做：

```text
1. RiskAgent
2. StateTracker
3. StrategyPlanner
```

暂时不优先做：

```text
ResponseAgent
OutputGuard
```

原因：回复生成和输出安全更依赖真实模型质量，容易引入风格、安全、幻觉问题。先把结构化 agent 稳住。

### 重点文件

```text
agents/risk_agent.py
agents/state_tracker.py
agents/strategy_planner.py
llm/structured_client.py
llm/structured_output.py
llm/exceptions.py
llm/model_registry.py
llm/retry_policy.py
prompts/risk_agent.md
prompts/state_tracker.md
prompts/strategy_planner.md
schemas/risk.py
schemas/state.py
schemas/strategy.py
tests/unit/test_model_backed_agents.py
tests/integration/test_model_assisted_turn_flow.py
scripts/smoke_model_assisted_database_corpus.py
```

### C 需要消费/生产的数据结构

#### RiskAgent

输入：

```text
RiskInput
- current_message: Message
- recent_messages: list[Message]
- current_risk_level: RiskLevel
```

输出：

```text
RiskResult
- risk_level: RiskLevel
- categories: list[str]
- needs_clarification: bool
- route: RiskRoute
- reason_codes: list[str]
- confidence: float
```

#### StateTracker

输入：

```text
StateTrackerInput
- current_message: Message
- previous_state: SessionState
- recent_messages: list[Message]
- feedback: FeedbackResult | None
```

输出：

```text
StateDelta
- explicit_user_request: str | None
- topic_updates: list[TopicUpdate]
- goal_updates: list[GoalUpdate]
- reported_emotions: list[EmotionReport]
- user_corrections: list[UserCorrection]
- strategy_preferences: list[StrategyPreference]
- hypotheses: list[Hypothesis]
```

注意：`StateTracker` 不能直接改 `SessionState`，只能返回 `StateDelta`。真正更新由 `StateReducer` 做。

#### StrategyPlanner

输入：

```text
StrategyPlannerInput
- current_message: Message
- context: ContextPackage
- risk: RiskResult
- feedback: FeedbackResult | None
```

输出：

```text
StrategyPlan
- conversation_phase: ConversationPhase
- primary_strategy: str
- objective: str
- reason: str
- avoid: list[str]
- expected_signals: list[str]
- switch_conditions: list[str]
```

### C 的开发重点

1. prompt 要强制模型只输出 JSON。
2. 输出必须能被 Pydantic schema 校验。
3. 失败时要 fallback，不要让一轮对话崩掉。
4. 所有 source id 必须来自输入 message，不能编造。
5. 不允许输出诊断、人格判断、隐藏动机推断。

### C 的验收命令

```bash
ruff check .
mypy .
pytest tests/unit/test_model_backed_agents.py
pytest tests/integration/test_model_assisted_turn_flow.py
python scripts/smoke_model_assisted_database_corpus.py --ascii
```

### 完成标准

C 完成后应该能说明：

```text
1. RiskAgent 可以用 Qwen 7B 返回合法 RiskResult。
2. StateTracker 可以用 Qwen 7B 返回合法 StateDelta。
3. StrategyPlanner 可以用 Qwen 7B 返回合法 StrategyPlan。
4. 模型输出不合法时 fallback 正常。
5. DB smoke 里 session_state_versions 和 intervention_events 内容合理。
```

## D：loop / summary / memory worker

### 简短目标

把当前手动 smoke 触发的 summary 和 memory 流程，整理成可复用的 loop/worker 逻辑；后续再把 fake finalizer/summarizer 替换成 model-backed 版本。

### D 已经有基础的部分

当前主线已经有：

```text
ConflictResolver
MemoryPolicy
FakeRollingSummarizer
FakeSessionFinalizer
SqlAlchemySummaryRepository
SqlAlchemyMemoryRepository
```

也已经跑通：

```text
rolling_summary_versions rows=1
long_term_memories rows=1
```

### D 接下来优先做

按顺序：

```text
1. SummaryWorker
2. SessionCloseMemoryWorker
3. model-backed RollingSummarizer
4. model-backed SessionFinalizer
5. MemoryCurator 更丰富的 candidate 生成逻辑
```

### 重点文件

```text
agents/rolling_summarizer.py
agents/session_finalizer.py
agents/memory_curator.py
services/conflict_resolver.py
services/memory_policy.py
storage/repositories/summary_repository.py
storage/repositories/memory_repository.py
storage/repositories/intervention_repository.py
workers/
schemas/summary.py
schemas/memory.py
scripts/smoke_model_assisted_database_corpus.py
scripts/smoke_session_close_memory.py
tests/unit/test_rolling_summarizer.py
tests/unit/test_session_finalizer.py
tests/unit/test_memory_policy.py
tests/integration/test_sqlalchemy_summary_repository.py
tests/integration/test_sqlalchemy_memory_repository.py
```

### D 需要消费/生产的数据结构

#### RollingSummarizer

输入：

```text
RollingSummarizerInput
- session_id: SessionId
- previous_summary: RollingSummary | None
- uncovered_messages: list[Message]
- current_state: SessionState
- interventions: list[InterventionRecord]
```

输出：

```text
RollingSummary
- session_id: SessionId
- summary_version: int
- covered_from: MessageId
- covered_to: MessageId
- current_problem: str | None
- session_goal: str | None
- important_user_statements: list[str]
- strategies_attempted: list[str]
- strategy_responses: list[str]
- open_questions: list[str]
- source_message_ids: list[MessageId]
```

#### SessionFinalizer

输入：

```text
SessionFinalizerInput
- session_id: SessionId
- messages: list[Message]
- final_state: SessionState
- interventions: list[InterventionRecord]
- rolling_summary: RollingSummary | None
```

输出：

```text
SessionFinalizerResult
- session_id: SessionId
- session_summary: str
- goal_updates: list[str]
- unfinished_topics: list[str]
- action_items: list[ActionItem]
- candidate_memories: list[MemoryCandidate]
- strategy_outcomes: list[StrategyOutcome]
- risk_events: list[str]
- source_message_ids: list[MessageId]
```

#### MemoryPolicy

输入：

```text
MemoryPolicyInput
- candidate: MemoryCandidate
- existing_memories: list[LongTermMemory]
- user_memory_enabled: bool
- session_id: SessionId | None
```

输出：

```text
MemoryPolicyDecision
- allowed: bool
- operation: MemoryOperation | None
- reason_codes: list[str]
- requires_user_confirmation: bool
- target_memory_id: MemoryId | None
- sanitized_content: str | None
```

#### MemoryRepository

输入：

```text
user_id: UserId
candidate: MemoryCandidate
decision: MemoryPolicyDecision
```

输出：

```text
MemoryWriteResult
- memory_id: MemoryId | None
- operation: MemoryOperation
- applied: bool
- reason_codes: list[str]
```

### D 的开发重点

1. worker 不要绕过 schemas。
2. worker 不要直接拼 dict 写库，要走 repository。
3. summary 和 memory 必须保留 source_message_ids。
4. model-backed finalizer 失败时必须 fallback 到 fake finalizer。
5. 不要把模型推断直接写成长记忆，必须经过 MemoryPolicy。

### D 的验收命令

```bash
ruff check .
mypy .
pytest tests/unit/test_rolling_summarizer.py tests/unit/test_session_finalizer.py tests/unit/test_memory_policy.py
pytest tests/integration/test_sqlalchemy_summary_repository.py tests/integration/test_sqlalchemy_memory_repository.py
python scripts/smoke_model_assisted_database_corpus.py --ascii
python scripts/smoke_session_close_memory.py --ascii
```

### 完成标准

D 完成后应该能说明：

```text
1. summary 可以由 worker 触发，而不是只在 smoke 脚本里手动触发。
2. session close memory 可以由 worker 触发。
3. long_term_memories 只写入 policy 允许的内容。
4. 所有 memory 都能追溯到 source_message_ids。
5. fake 和 model-backed 版本可以互相替换。
```

## 当前建议的协作顺序

```text
B 先让服务器、数据库、Qwen 7B endpoint 跑起来。
C 在 B 的 endpoint 上调 RiskAgent / StateTracker / StrategyPlanner。
D 先把 summary/memory worker 化，同时准备 model-backed summarizer/finalizer。
A 负责收口集成、跑 smoke、检查表内容和决定什么时候替换 fake。
```

## 当前不要急着做的事

```text
1. 不要急着替换 ResponseAgent。
2. 不要绕过 StructuredLLMClient 直接散落调用模型。
3. 不要让 agent 直接写数据库。
4. 不要让模型推断直接进入 long_term_memories。
5. 不要改 schemas，除非确实发现现有合同表达不了必要数据。
```