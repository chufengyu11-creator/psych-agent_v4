# 心理支持对话 Agent：程序设计与代码架构

> 本文给出推荐的代码仓库结构、文件职责、关键接口、数据 Schema、存储结构和调用关系。  
> 建议使用 Python、FastAPI、Pydantic、PostgreSQL、pgvector、Redis 和后台任务队列。

---

## 1. 推荐技术栈

### 后端

- Python 3.11+
- FastAPI
- Pydantic v2
- SQLAlchemy 2.x
- Alembic
- PostgreSQL
- pgvector
- Redis
- Celery / RQ / Dramatiq
- vLLM 或兼容 OpenAI API 的模型服务

### 测试与工程

- pytest
- pytest-asyncio
- mypy
- ruff
- pre-commit
- structured logging
- OpenTelemetry，可后续加入

---

## 2. 推荐仓库结构

```text
psych_agent/
├── pyproject.toml
├── README.md
├── .env.example
├── alembic.ini
│
├── app/
│   ├── main.py
│   ├── config.py
│   ├── dependencies.py
│   └── lifecycle.py
│
├── api/
│   ├── routers/
│   │   ├── chat.py
│   │   ├── sessions.py
│   │   ├── memories.py
│   │   ├── users.py
│   │   └── health.py
│   ├── request_models.py
│   └── response_models.py
│
├── orchestrator/
│   ├── turn_orchestrator.py
│   ├── safety_router.py
│   ├── session_lifecycle.py
│   ├── post_turn_pipeline.py
│   └── exceptions.py
│
├── agents/
│   ├── base.py
│   ├── risk_agent.py
│   ├── state_tracker.py
│   ├── feedback_evaluator.py
│   ├── strategy_planner.py
│   ├── response_agent.py
│   ├── output_guard.py
│   ├── rolling_summarizer.py
│   ├── session_finalizer.py
│   └── memory_curator.py
│
├── services/
│   ├── context_builder.py
│   ├── state_reducer.py
│   ├── memory_retriever.py
│   ├── memory_policy.py
│   ├── conflict_resolver.py
│   ├── knowledge_retriever.py
│   ├── embedding_service.py
│   ├── reranker_service.py
│   ├── token_budget.py
│   └── audit_logger.py
│
├── storage/
│   ├── database.py
│   ├── models/
│   │   ├── user.py
│   │   ├── session.py
│   │   ├── message.py
│   │   ├── session_state.py
│   │   ├── summary.py
│   │   ├── memory.py
│   │   ├── goal.py
│   │   ├── intervention.py
│   │   ├── risk_event.py
│   │   ├── consent.py
│   │   └── agent_call_log.py
│   ├── repositories/
│   │   ├── user_repository.py
│   │   ├── session_repository.py
│   │   ├── message_repository.py
│   │   ├── state_repository.py
│   │   ├── summary_repository.py
│   │   ├── memory_repository.py
│   │   ├── intervention_repository.py
│   │   └── audit_repository.py
│   └── migrations/
│
├── schemas/
│   ├── common.py
│   ├── messages.py
│   ├── risk.py
│   ├── state.py
│   ├── feedback.py
│   ├── strategy.py
│   ├── context.py
│   ├── summary.py
│   ├── memory.py
│   ├── intervention.py
│   └── safety.py
│
├── prompts/
│   ├── system/
│   │   ├── product_boundary.md
│   │   └── response_policy.md
│   ├── risk_agent.md
│   ├── state_tracker.md
│   ├── feedback_evaluator.md
│   ├── strategy_planner.md
│   ├── response_agent.md
│   ├── output_guard.md
│   ├── rolling_summarizer.md
│   ├── session_finalizer.md
│   └── memory_curator.md
│
├── llm/
│   ├── client.py
│   ├── model_registry.py
│   ├── structured_output.py
│   ├── retry_policy.py
│   ├── tool_registry.py
│   └── exceptions.py
│
├── workers/
│   ├── worker_app.py
│   ├── post_turn_worker.py
│   ├── summary_worker.py
│   ├── memory_worker.py
│   ├── session_close_worker.py
│   └── evaluation_worker.py
│
├── policies/
│   ├── safety_policy.yaml
│   ├── memory_policy.yaml
│   ├── context_policy.yaml
│   └── strategy_policy.yaml
│
├── evaluation/
│   ├── datasets/
│   │   ├── state_tracking/
│   │   ├── summary_faithfulness/
│   │   ├── memory_consistency/
│   │   ├── strategy_switching/
│   │   └── safety/
│   ├── evaluators/
│   │   ├── state_evaluator.py
│   │   ├── summary_evaluator.py
│   │   ├── memory_evaluator.py
│   │   ├── strategy_evaluator.py
│   │   └── safety_evaluator.py
│   ├── run_eval.py
│   └── reports/
│
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── contract/
│   └── end_to_end/
│
└── scripts/
    ├── create_admin_user.py
    ├── rebuild_summaries.py
    ├── rebuild_user_state.py
    ├── export_audit_logs.py
    └── seed_evaluation_cases.py
```

---

## 3. 顶层文件职责

## 3.1 `app/main.py`

负责：

- 创建 FastAPI 应用；
- 注册路由；
- 注册异常处理器；
- 初始化依赖；
- 启动与关闭生命周期。

```python
from fastapi import FastAPI

from api.routers import chat, sessions, memories, health


def create_app() -> FastAPI:
    app = FastAPI(title="Psychological Support Agent")

    app.include_router(chat.router, prefix="/chat", tags=["chat"])
    app.include_router(sessions.router, prefix="/sessions", tags=["sessions"])
    app.include_router(memories.router, prefix="/memories", tags=["memories"])
    app.include_router(health.router, prefix="/health", tags=["health"])

    return app


app = create_app()
```

## 3.2 `app/config.py`

负责集中读取：

- 数据库 URL；
- Redis URL；
- 模型服务地址；
- 模型名称；
- token 预算；
- 摘要触发阈值；
- 功能开关；
- 日志等级。

建议使用 `pydantic-settings`。

## 3.3 `app/dependencies.py`

负责 FastAPI 依赖注入：

- 获取数据库 Session；
- 获取当前用户；
- 获取 Orchestrator；
- 获取 repositories；
- 获取模型客户端。

---

## 4. API 层

## 4.1 `api/routers/chat.py`

提供：

```text
POST /chat/turn
POST /chat/stream
POST /chat/end-session
```

请求示例：

```json
{
  "user_id": "user_123",
  "session_id": "session_456",
  "message": "我今天有点焦虑"
}
```

响应示例：

```json
{
  "message_id": "msg_789",
  "response": "……",
  "session_id": "session_456",
  "status": "ok"
}
```

API 层只负责协议转换，不处理 Agent 逻辑。

## 4.2 `api/routers/memories.py`

提供：

```text
GET    /memories
PATCH  /memories/{memory_id}
DELETE /memories/{memory_id}
POST   /memories/{memory_id}/confirm
```

用户必须能够查看、修改、删除和确认长期记忆。

## 4.3 `api/routers/sessions.py`

提供：

```text
POST /sessions
GET  /sessions/{session_id}
POST /sessions/{session_id}/close
GET  /sessions/{session_id}/summary
```

---

## 5. Orchestrator 层

## 5.1 `orchestrator/turn_orchestrator.py`

这是单轮处理的核心入口。

```python
class TurnOrchestrator:
    def __init__(
        self,
        risk_agent,
        state_tracker,
        feedback_evaluator,
        strategy_planner,
        response_agent,
        output_guard,
        state_reducer,
        context_builder,
        memory_retriever,
        message_repository,
        state_repository,
        summary_repository,
        intervention_repository,
        task_queue,
    ):
        ...

    async def handle_turn(
        self,
        user_id: str,
        session_id: str,
        text: str,
    ) -> str:
        ...
```

### 推荐执行顺序

```python
async def handle_turn(...):
    user_message = await message_repository.create_user_message(...)

    pending = await intervention_repository.get_pending(session_id)

    feedback = None
    if pending:
        feedback = await feedback_evaluator.evaluate(
            intervention=pending,
            user_message=user_message,
        )
        await intervention_repository.complete(
            intervention_id=pending.id,
            feedback=feedback,
        )

    risk_result, state_delta = await asyncio.gather(
        risk_agent.analyze(...),
        state_tracker.extract_delta(...),
    )

    if risk_result.route != "normal_dialogue":
        return await safety_router.handle(...)

    previous_state = await state_repository.get_current(session_id)

    current_state = state_reducer.apply(
        previous_state=previous_state,
        delta=state_delta,
        feedback=feedback,
        risk=risk_result,
    )

    await state_repository.save_version(current_state)

    memories = await memory_retriever.retrieve(
        user_id=user_id,
        query=text,
        session_state=current_state,
    )

    strategy = await strategy_planner.plan(
        session_state=current_state,
        feedback=feedback,
        memories=memories,
    )

    context = await context_builder.build(
        session_state=current_state,
        rolling_summary=await summary_repository.get_current(session_id),
        recent_messages=await message_repository.get_recent(session_id),
        memories=memories,
        strategy=strategy,
        risk=risk_result,
    )

    draft = await response_agent.generate(context)

    guard_result = await output_guard.review(
        draft=draft,
        context=context,
        risk=risk_result,
    )

    final_text = guard_result.final_text

    assistant_message = await message_repository.create_assistant_message(
        session_id=session_id,
        text=final_text,
    )

    await intervention_repository.create_pending(
        session_id=session_id,
        assistant_message_id=assistant_message.id,
        strategy=strategy.primary_strategy,
        objective=strategy.objective,
        expected_signals=strategy.expected_signals,
    )

    await task_queue.enqueue(
        "post_turn",
        user_id=user_id,
        session_id=session_id,
    )

    return final_text
```

## 5.2 `orchestrator/safety_router.py`

负责根据 `risk_result.route` 进入：

- 普通安全澄清；
- 固定危机流程；
- 真人转接；
- 限制某些工具调用。

它不应依赖主对话模型自由决定所有内容。

## 5.3 `orchestrator/session_lifecycle.py`

负责：

- 创建 Session；
- 恢复 Session；
- 关闭 Session；
- 触发 Session Finalizer；
- 下一次会话 Bootstrap。

## 5.4 `orchestrator/post_turn_pipeline.py`

负责发布后台任务：

- 更新滚动摘要；
- 提取候选记忆；
- 检查冲突；
- 写入评测样本；
- 记录审计日志。

---

## 6. Agent 层

## 6.1 `agents/base.py`

定义统一接口：

```python
from typing import Generic, TypeVar

InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")


class BaseAgent(Generic[InputT, OutputT]):
    async def run(self, payload: InputT) -> OutputT:
        raise NotImplementedError
```

也可增加：

- `model_name`
- `prompt_version`
- `timeout`
- `max_retries`
- `temperature`
- `response_schema`

## 6.2 `agents/risk_agent.py`

```python
class RiskAgent(BaseAgent[RiskInput, RiskResult]):
    async def analyze(self, payload: RiskInput) -> RiskResult:
        ...
```

要求结构化输出，并在解析失败时回退到保守结果。

## 6.3 `agents/state_tracker.py`

```python
class StateTracker(BaseAgent[StateTrackerInput, StateDelta]):
    async def extract_delta(
        self,
        payload: StateTrackerInput,
    ) -> StateDelta:
        ...
```

只输出增量，不写数据库。

## 6.4 `agents/feedback_evaluator.py`

```python
class FeedbackEvaluator(
    BaseAgent[FeedbackInput, FeedbackResult]
):
    async def evaluate(
        self,
        payload: FeedbackInput,
    ) -> FeedbackResult:
        ...
```

## 6.5 `agents/strategy_planner.py`

```python
class StrategyPlanner(
    BaseAgent[StrategyPlannerInput, StrategyPlan]
):
    async def plan(
        self,
        payload: StrategyPlannerInput,
    ) -> StrategyPlan:
        ...
```

## 6.6 `agents/response_agent.py`

```python
class ResponseAgent(
    BaseAgent[ResponseContext, DraftResponse]
):
    async def generate(
        self,
        context: ResponseContext,
    ) -> DraftResponse:
        ...
```

## 6.7 `agents/output_guard.py`

```python
class OutputGuard(
    BaseAgent[GuardInput, GuardResult]
):
    async def review(
        self,
        payload: GuardInput,
    ) -> GuardResult:
        ...
```

## 6.8 `agents/rolling_summarizer.py`

负责增量更新摘要。

## 6.9 `agents/session_finalizer.py`

负责会话结束后的结构化总结。

## 6.10 `agents/memory_curator.py`

负责生成候选长期记忆与建议操作。

---

## 7. Service 层

## 7.1 `services/state_reducer.py`

必须是确定性逻辑。

```python
class StateReducer:
    def apply(
        self,
        previous_state: SessionState,
        delta: StateDelta,
        feedback: FeedbackResult | None,
        risk: RiskResult,
    ) -> SessionState:
        ...
```

建议内部拆成：

- `_apply_topic_updates`
- `_apply_goal_updates`
- `_apply_user_corrections`
- `_apply_strategy_preferences`
- `_apply_risk_state`
- `_increment_version`

## 7.2 `services/context_builder.py`

```python
class ContextBuilder:
    async def build(
        self,
        session_state: SessionState,
        rolling_summary: RollingSummary | None,
        recent_messages: list[Message],
        memories: RetrievedMemories,
        strategy: StrategyPlan,
        risk: RiskResult,
    ) -> ResponseContext:
        ...
```

Context Builder 应依赖 `TokenBudgetManager`。

## 7.3 `services/token_budget.py`

统一管理每块 context 的 token 上限：

```python
DEFAULT_BUDGET = {
    "system_policy": 1200,
    "risk_state": 300,
    "session_state": 800,
    "strategy_plan": 400,
    "rolling_summary": 1200,
    "recent_messages": 3000,
    "long_term_memory": 800,
    "knowledge": 1200,
}
```

## 7.4 `services/memory_retriever.py`

建议分两步：

1. 规则过滤；
2. 向量检索与 rerank。

```python
class MemoryRetriever:
    async def retrieve(
        self,
        user_id: str,
        query: str,
        session_state: SessionState,
        limit: int = 8,
    ) -> RetrievedMemories:
        ...
```

## 7.5 `services/memory_policy.py`

```python
class MemoryPolicy:
    def evaluate_candidate(
        self,
        candidate: MemoryCandidate,
        user_consent: ConsentState,
        existing_memories: list[LongTermMemory],
    ) -> MemoryPolicyDecision:
        ...
```

## 7.6 `services/conflict_resolver.py`

处理：

- 新旧状态矛盾；
- 旧记忆被替代；
- 多条记忆含义重复；
- 用户明确纠正历史信息。

## 7.7 `services/audit_logger.py`

统一写入 Agent 调用日志，避免各模块自己散乱记录。

---

## 8. Schema 设计

## 8.1 `schemas/risk.py`

```python
from enum import Enum
from pydantic import BaseModel, Field


class RiskRoute(str, Enum):
    NORMAL = "normal_dialogue"
    CLARIFICATION = "safety_clarification"
    CRISIS = "crisis_protocol"
    HUMAN = "human_escalation"


class RiskResult(BaseModel):
    risk_level: str
    categories: list[str] = Field(default_factory=list)
    needs_clarification: bool = False
    route: RiskRoute
    reason_codes: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
```

## 8.2 `schemas/state.py`

```python
class StateOperation(str, Enum):
    ADD = "add"
    UPDATE = "update"
    REMOVE = "remove"
    RESOLVE_CONFLICT = "resolve_conflict"


class TopicUpdate(BaseModel):
    operation: StateOperation
    topic: str
    source_message_id: str


class StateDelta(BaseModel):
    explicit_user_request: str | None = None
    topic_updates: list[TopicUpdate] = Field(default_factory=list)
    goal_updates: list[dict] = Field(default_factory=list)
    reported_emotions: list[dict] = Field(default_factory=list)
    user_corrections: list[dict] = Field(default_factory=list)
    strategy_preferences: list[dict] = Field(default_factory=list)
    hypotheses: list[dict] = Field(default_factory=list)
```

## 8.3 `schemas/strategy.py`

```python
class StrategyPlan(BaseModel):
    conversation_phase: str
    primary_strategy: str
    objective: str
    reason: str
    avoid: list[str] = Field(default_factory=list)
    expected_signals: list[str] = Field(default_factory=list)
    switch_conditions: list[str] = Field(default_factory=list)
```

## 8.4 `schemas/intervention.py`

```python
class InterventionStatus(str, Enum):
    PENDING = "pending"
    EVALUATED = "evaluated"
    CANCELLED = "cancelled"


class InterventionRecord(BaseModel):
    intervention_id: str
    session_id: str
    assistant_message_id: str
    strategy: str
    objective: str
    expected_signals: list[str]
    status: InterventionStatus
    observed_response: str | None = None
    strategy_fit: str | None = None
    objective_progress: str | None = None
```

## 8.5 `schemas/memory.py`

```python
class MemoryOperation(str, Enum):
    CREATE = "CREATE"
    REINFORCE = "REINFORCE"
    SUPERSEDE = "SUPERSEDE"
    MARK_CONFLICT = "MARK_CONFLICT"
    EXPIRE = "EXPIRE"
    DELETE = "DELETE"


class MemoryCandidate(BaseModel):
    candidate_type: str
    content: str
    source_message_ids: list[str]
    source_type: str
    confidence: float
    requires_user_confirmation: bool
    sensitivity: str
    recommended_operation: MemoryOperation
```

---

## 9. 数据库表设计

## 9.1 `users`

关键字段：

```text
id
created_at
status
locale
timezone
memory_enabled
deleted_at
```

## 9.2 `sessions`

```text
id
user_id
status
started_at
ended_at
current_state_version
current_summary_version
```

## 9.3 `messages`

```text
id
session_id
role
content
created_at
sequence_number
model_name
parent_message_id
```

原始消息建议 append-only。

## 9.4 `session_state_versions`

```text
id
session_id
version
state_json
created_at
source_message_id
previous_version_id
```

## 9.5 `rolling_summary_versions`

```text
id
session_id
version
summary_json
covered_from_message_id
covered_to_message_id
previous_version_id
created_at
```

## 9.6 `long_term_memories`

```text
id
user_id
memory_type
content
embedding
source_type
source_message_ids
status
confidence
sensitivity
user_confirmed
valid_from
valid_to
supersedes_memory_id
created_at
updated_at
```

## 9.7 `goals`

```text
id
user_id
session_id
content
status
user_confirmed
created_at
updated_at
closed_at
```

## 9.8 `intervention_events`

```text
id
session_id
assistant_message_id
strategy
objective
expected_signals
status
observed_response
explicit_feedback
strategy_fit
objective_progress
recommended_adjustment
created_at
evaluated_at
```

## 9.9 `risk_events`

```text
id
user_id
session_id
message_id
risk_level
categories
route
reason_codes
model_version
created_at
```

## 9.10 `consent_records`

```text
id
user_id
consent_type
granted
policy_version
created_at
revoked_at
```

## 9.11 `agent_call_logs`

```text
id
session_id
message_id
agent_name
model_name
prompt_version
input_hash
output_json
latency_ms
status
error_code
created_at
```

---

## 10. Repository 层

Repository 负责数据库访问，不包含业务逻辑。

示例：

```python
class MessageRepository:
    async def create_user_message(...): ...
    async def create_assistant_message(...): ...
    async def get_recent(...): ...
    async def get_range(...): ...
```

```python
class StateRepository:
    async def get_current(...): ...
    async def save_version(...): ...
    async def rebuild_from_events(...): ...
```

```python
class MemoryRepository:
    async def search_active(...): ...
    async def create(...): ...
    async def supersede(...): ...
    async def mark_conflict(...): ...
    async def delete_for_user(...): ...
```

---

## 11. LLM 客户端层

## 11.1 `llm/client.py`

统一封装模型调用：

```python
class LLMClient:
    async def generate_text(...): ...
    async def generate_structured(...): ...
    async def stream_text(...): ...
```

## 11.2 `llm/model_registry.py`

定义不同 Agent 使用哪个模型：

```python
MODEL_REGISTRY = {
    "risk_agent": "small-safety-model",
    "state_tracker": "small-structured-model",
    "strategy_planner": "reasoning-model",
    "response_agent": "main-dialogue-model",
    "summarizer": "small-summary-model",
}
```

## 11.3 `llm/structured_output.py`

负责：

- JSON Schema；
- Pydantic 验证；
- 自动修复一次；
- 失败回退；
- 记录原始输出。

## 11.4 `llm/retry_policy.py`

针对：

- 超时；
- 非法 JSON；
- 空输出；
- 模型服务不可用；
- 内容过长。

---

## 12. Prompt 管理

每个 Prompt 文件应包含：

1. 任务定义；
2. 输入字段说明；
3. 不允许做的事情；
4. 输出 JSON Schema；
5. 正反例；
6. prompt version。

建议 Prompt 不直接硬编码在 Python 文件中。

示例：

```text
prompts/
  state_tracker.md
  state_tracker.v2.md
```

或者使用数据库/配置中心统一版本化。

---

## 13. 后台任务

## 13.1 `workers/post_turn_worker.py`

负责：

- 检查是否需要滚动摘要；
- 调用 Memory Curator；
- 写审计日志；
- 写评测事件。

## 13.2 `workers/summary_worker.py`

负责：

- 读取旧摘要；
- 读取未覆盖消息；
- 生成新版本；
- 做忠实度检查；
- 落库。

## 13.3 `workers/memory_worker.py`

负责：

- 生成候选长期记忆；
- 执行 Memory Policy；
- 去重和冲突判断；
- 写入或等待用户确认。

## 13.4 `workers/session_close_worker.py`

负责：

- Session Finalizer；
- 更新 active goals；
- 生成 unfinished topics；
- 更新跨会话状态。

---

## 14. 事件与任务设计

建议至少定义以下内部事件：

```text
UserMessageCreated
FeedbackEvaluated
RiskEvaluated
SessionStateUpdated
StrategyPlanned
AssistantMessageCreated
InterventionCreated
RollingSummaryRequested
RollingSummaryUpdated
MemoryCandidateCreated
MemoryConfirmed
SessionClosed
```

可以先用普通函数调用和任务队列，后续再迁移到真正的事件总线。

---

## 15. Tool Calling 代码边界

## 15.1 工具注册

`llm/tool_registry.py`

```python
READ_ONLY_TOOLS = {
    "retrieve_relevant_memory",
    "get_active_goals",
    "get_previous_session_summary",
    "retrieve_psychology_knowledge",
}
```

## 15.2 不向主模型开放的工具

```text
write_long_term_memory
delete_memory
set_risk_level
skip_output_guard
update_model_weights
read_all_sensitive_history
```

写操作必须走 Service 和 Policy。

---

## 16. 错误处理与回退

## Risk Agent 失败

- 默认进入更保守的澄清路径；
- 不直接继续普通回复。

## State Tracker 失败

- 保留旧 Session State；
- 当前消息仍保存在原始消息表；
- 记录错误，稍后可重放。

## Strategy Planner 失败

- 回退到安全的通用策略，例如 `clarification` 或 `reflective_listening`。

## Response Agent 失败

- 重试一次；
- 切换备用模型；
- 返回受控的系统错误提示。

## Output Guard 失败

- 不直接发送未经审核的回复；
- 使用保守回退回复。

## 后台摘要失败

- 不影响当前对话；
- 记录任务失败并重试；
- Context Builder 继续使用旧摘要和最近消息。

---

## 17. 测试结构

## 17.1 Unit Tests

覆盖：

- State Reducer；
- Memory Policy；
- Context Builder；
- Token Budget；
- Conflict Resolver；
- Repository。

## 17.2 Contract Tests

验证每个 Agent 的输入输出符合 Pydantic Schema。

## 17.3 Integration Tests

验证：

- 单轮完整链路；
- 状态更新；
- 摘要触发；
- 记忆候选生成；
- 策略切换；
- 安全路由。

## 17.4 End-to-End Tests

模拟：

- 20 轮以上长对话；
- 多次 Session；
- 用户纠正系统；
- 用户删除记忆；
- 历史信息冲突；
- 上轮策略被明确拒绝；
- 风险场景切换。

---

## 18. 推荐的第一阶段实现顺序

### Milestone 1：单次会话主链路

实现：

- messages；
- Session State；
- Risk Agent；
- State Tracker；
- State Reducer；
- Strategy Planner；
- Response Agent；
- Output Guard；
- Turn Orchestrator。

### Milestone 2：会话内长期运行

实现：

- Rolling Summary；
- token budget；
- Intervention Ledger；
- Feedback Evaluator；
- Strategy switching。

### Milestone 3：跨会话

实现：

- Session Finalizer；
- active goals；
- unfinished topics；
-少量长期记忆；
- 下一次 Session Bootstrap。

### Milestone 4：记忆治理

实现：

- Memory Curator；
- Memory Policy；
- 用户确认；
- 查看、修改、删除；
- 冲突与替代。

### Milestone 5：评测与模型替换

实现：

- 固定评测集；
- Agent contract tests；
- 9B 与强模型对比；
- Prompt 版本和模型版本管理。

---

## 19. 最小可运行版本的文件集合

第一版不必一次创建整个仓库。最少可以先创建：

```text
app/main.py
api/routers/chat.py
orchestrator/turn_orchestrator.py
agents/risk_agent.py
agents/state_tracker.py
agents/strategy_planner.py
agents/response_agent.py
agents/output_guard.py
services/state_reducer.py
services/context_builder.py
storage/database.py
storage/repositories/message_repository.py
storage/repositories/state_repository.py
storage/repositories/intervention_repository.py
schemas/risk.py
schemas/state.py
schemas/strategy.py
schemas/intervention.py
llm/client.py
tests/integration/test_turn_flow.py
```

在主链路稳定后，再增加摘要、长期记忆和跨会话模块。

---

## 20. 最终程序设计原则

1. Orchestrator 控制流程；
2. Agent 只做单一职责；
3. 所有结构化输出必须经过 Schema 校验；
4. 状态更新必须经过 Reducer；
5. 长期记忆写入必须经过 Policy；
6. 原始消息 append-only；
7. 摘要与状态必须版本化；
8. 每次模型调用必须可审计；
9. 高风险流程不得依赖主模型自由发挥；
10. 模型可替换，状态系统不可随模型变化而重写。
