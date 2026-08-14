# 细化分工任务书

> 这份文档用于直接发给同事。
>
> 每个任务都写清楚：
>
> 1. 要修改哪些文件；
> 2. 要写哪些类或函数；
> 3. 每个函数消费什么数据类型、返回什么数据类型；
> 4. 这个任务要达成什么目的。
>
> 当前约定：A 是整合负责人，也就是你自己。D 的任务仍然只写 D，A 私下如何帮 D 不写进这里。
>
> 如果看到 `SessionState`、`ResponseContext`、`MemoryCandidate` 这类抽象类型，字段级类型请看：
> `docs/schema_field_reference.md`。

## 0. 总体原则

### 0.1 不要随便改公共 contract

这些文件属于公共接口：

```text
schemas/
orchestrator/turn_orchestrator.py
docs/contracts/README.md
```

如果必须改，需要单独说明原因。

### 0.2 本项目主接入方式不是 HTTP API

后续主接入方式是本地部署里的 Python 调用。

主入口是：

```python
await TurnOrchestrator.handle_turn(
    user_id=UserId(...),
    session_id=SessionId(...),
    text="...",
)
```

HTTP API 只作为本地调试或 demo，不作为主要开发目标。

### 0.3 每个任务完成后必须跑

```bash
ruff check .
mypy .
pytest
```

## 1. A：Contract / 主流程 / Context / 整合负责人

A 是你自己，主要负责守住公共结构和主链路。

### A 的目的

```text
保证所有人的模块能接起来。
保证数据结构稳定。
保证主流程不被各模块绕过。
保证 context 管理清楚。
```

### A1. 维护 schemas 和主流程 contract

负责文件：

```text
schemas/
orchestrator/turn_orchestrator.py
docs/contracts/README.md
docs/data_structure_contracts.md
```

需要维护的核心数据类型：

```text
Message
RiskInput
RiskResult
StateTrackerInput
StateDelta
SessionState
FeedbackInput
FeedbackResult
StrategyPlannerInput
StrategyPlan
ResponseContext
DraftResponse
GuardInput
GuardResult
RollingSummarizerInput
RollingSummary
SessionFinalizerInput
SessionFinalizerResult
MemoryCandidate
MemoryPolicyInput
MemoryPolicyDecision
PostTurnEvent
SessionCloseRequestedEvent
```

主要职责：

```text
审核同事是否新增了重复的数据结构
发现字段不够时统一扩展 schemas/
不让各模块使用自己临时发明的 dict shape
```

验收标准：

```text
所有 schema 能被 import
mypy 无类型错误
contract tests 通过
```

### A2. 维护 TurnOrchestrator 主链路

负责文件：

```text
orchestrator/turn_orchestrator.py
tests/integration/test_turn_flow.py
```

当前主函数：

```python
async def handle_turn(
    self,
    user_id: UserId,
    session_id: SessionId,
    text: str,
) -> ChatTurnResult:
    ...
```

消费的数据：

```text
UserId
SessionId
str
```

返回的数据：

```text
ChatTurnResult
```

内部必须按顺序消费和产生：

```text
Message
FeedbackResult | None
RiskResult
StateDelta
SessionState
RetrievedMemories
StrategyPlan
ResponseContext
DraftResponse
GuardResult
Message
InterventionRecord
```

目的：

```text
控制一轮对话完整生命周期，确保状态、策略、回复、安全检查顺序稳定。
```

关键约束：

```text
ResponseAgent 不能直接写数据库
StateTracker 不能直接改 SessionState
所有状态更新必须经过 StateReducer
所有回复必须经过 OutputGuard
高风险 route 不能继续普通回复链路
```

### A3. Context 管理

负责文件：

```text
services/context_builder.py
services/token_budget.py
schemas/context.py
tests/unit/test_context_builder.py
```

需要维护的函数：

```python
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

消费的数据类型：

```text
SessionState
RollingSummary | None
list[Message]
RetrievedMemories
StrategyPlan
RiskResult
```

返回的数据类型：

```text
ResponseContext
```

`ResponseContext` 里应该包含：

```text
system_policy: str
risk: RiskResult
session_state: SessionState
strategy: StrategyPlan
recent_messages: list[Message]
rolling_summary: RollingSummary | None
memories: RetrievedMemories
sections: list[ContextSection]
```

目的：

```text
决定本轮要喂给回复模型什么信息，以及这些信息的优先级。
```

关键规则：

```text
当前原始消息优先于 summary
当前状态优先于长期 memory
ContextBuilder 不写数据库
ContextBuilder 不决定新 memory 是否创建
```

### A4. 本地部署 adapter

建议新增文件：

```text
local_runtime/__init__.py
local_runtime/adapter.py
local_runtime/factory.py
tests/integration/test_local_runtime.py
```

建议新增类：

```python
class LocalPsychAgent:
    async def handle_message(
        self,
        user_id: str,
        session_id: str,
        text: str,
    ) -> ChatTurnResult:
        ...
```

消费的数据类型：

```text
str user_id
str session_id
str text
```

内部转换成：

```text
UserId
SessionId
```

返回的数据类型：

```text
ChatTurnResult
```

目的：

```text
让本地部署系统不启动 FastAPI，也能直接调用 agent。
```

## 2. B：Storage / Repository / 本地持久化负责人

### B 的目的

```text
把 in-memory repositories 替换成真实本地持久化，同时保持上层接口不变。
```

B 不决定心理语义，不决定什么应该被记住。B 只负责可靠存取。

## B1. 数据库基础设施

负责文件：

```text
storage/database.py
app/config.py
alembic.ini
.env.example
```

需要写的类型或函数：

```python
class Settings(BaseSettings):
    database_url: str
    ...
```

```python
def create_engine(settings: Settings) -> AsyncEngine:
    ...
```

```python
def create_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    ...
```

```python
async def get_session() -> AsyncIterator[AsyncSession]:
    ...
```

消费的数据类型：

```text
Settings
AsyncEngine
```

返回的数据类型：

```text
AsyncEngine
async_sessionmaker[AsyncSession]
AsyncIterator[AsyncSession]
```

目的：

```text
给 repository 提供统一数据库连接，不让 agent 或 orchestrator 直接碰数据库。
```

## B2. 第一阶段数据库 models

负责文件：

```text
storage/models/user.py
storage/models/session.py
storage/models/message.py
storage/models/session_state.py
storage/models/intervention.py
storage/models/summary.py
storage/models/memory.py
storage/models/__init__.py
```

需要实现的表：

```text
users
sessions
messages
session_state_versions
intervention_events
rolling_summary_versions
long_term_memories
```

每张表的目的：

```text
users：用户基础信息
sessions：一次会话
messages：append-only 原始消息
session_state_versions：SessionState 版本
intervention_events：策略和反馈记录
rolling_summary_versions：长对话摘要版本
long_term_memories：长期记忆
```

关键字段建议：

```text
messages:
- id
- session_id
- role
- content
- sequence_number
- created_at
- model_name
- parent_message_id

session_state_versions:
- id
- session_id
- version
- state_json
- source_message_id
- created_at

intervention_events:
- id
- session_id
- assistant_message_id
- strategy
- objective
- expected_signals
- status
- observed_response
- explicit_feedback
- strategy_fit
- objective_progress
- recommended_adjustment
- created_at
- evaluated_at

rolling_summary_versions:
- id
- session_id
- summary_version
- summary_json
- covered_from
- covered_to
- created_at

long_term_memories:
- id
- user_id
- memory_type
- content
- sensitivity
- confidence
- source_message_ids
- status
- created_at
- updated_at
```

目的：

```text
把当前 Pydantic schemas 持久化到本地数据库里。
```

## B3. MessageRepository

负责文件：

```text
storage/repositories/message_repository.py
tests/unit/test_message_repository.py
```

必须保留或实现的函数：

```python
async def create_user_message(
    self,
    session_id: SessionId,
    text: str,
) -> Message:
    ...
```

```python
async def create_assistant_message(
    self,
    session_id: SessionId,
    text: str,
) -> Message:
    ...
```

```python
async def get_recent(
    self,
    session_id: SessionId,
    limit: int = 12,
) -> list[Message]:
    ...
```

```python
async def get_by_id(
    self,
    message_id: MessageId,
) -> Message | None:
    ...
```

消费的数据类型：

```text
SessionId
MessageId
str
int
```

返回的数据类型：

```text
Message
list[Message]
Message | None
```

目的：

```text
存取原始消息。Message 是所有状态、summary、memory 的事实来源。
```

关键规则：

```text
messages append-only
不要修改历史 message content
返回 Pydantic Message，不返回 SQLAlchemy model
```

## B4. StateRepository

负责文件：

```text
storage/repositories/state_repository.py
tests/unit/test_state_repository.py
```

必须实现的函数：

```python
async def get_current(
    self,
    session_id: SessionId,
) -> SessionState:
    ...
```

```python
async def save_version(
    self,
    state: SessionState,
) -> None:
    ...
```

消费的数据类型：

```text
SessionId
SessionState
```

返回的数据类型：

```text
SessionState
None
```

目的：

```text
保存和读取当前 session 的版本化状态。
```

关键规则：

```text
不要覆盖旧版本
每个新 state 都作为新版本保存
```

## B5. InterventionRepository

负责文件：

```text
storage/repositories/intervention_repository.py
tests/unit/test_intervention_repository.py
```

必须实现的函数：

```python
async def get_pending(
    self,
    session_id: SessionId,
) -> InterventionRecord | None:
    ...
```

```python
async def create_pending(
    self,
    session_id: SessionId,
    assistant_message_id: MessageId,
    strategy: str,
    objective: str,
    expected_signals: list[str],
) -> InterventionRecord:
    ...
```

```python
async def complete(
    self,
    intervention_id: InterventionId,
    feedback: FeedbackResult,
) -> None:
    ...
```

消费的数据类型：

```text
SessionId
MessageId
InterventionId
FeedbackResult
str
list[str]
```

返回的数据类型：

```text
InterventionRecord | None
InterventionRecord
None
```

目的：

```text
记录上一轮 assistant 用了什么策略，并在下一轮接收用户反馈后完成评估。
```

## B6. SummaryRepository

负责文件：

```text
storage/repositories/summary_repository.py
tests/unit/test_summary_repository.py
```

建议实现的函数：

```python
async def get_current(
    self,
    session_id: SessionId,
) -> RollingSummary | None:
    ...
```

```python
async def save_version(
    self,
    summary: RollingSummary,
) -> None:
    ...
```

消费的数据类型：

```text
SessionId
RollingSummary
```

返回的数据类型：

```text
RollingSummary | None
None
```

目的：

```text
保存长对话 rolling summary，让 ContextBuilder 可以读取。
```

## B7. MemoryRepository

负责文件：

```text
storage/repositories/memory_repository.py
tests/unit/test_memory_repository.py
```

建议实现的函数：

```python
async def search_active(
    self,
    user_id: UserId,
    query: str,
    limit: int = 8,
) -> list[LongTermMemory]:
    ...
```

```python
async def create(
    self,
    user_id: UserId,
    candidate: MemoryCandidate,
) -> LongTermMemory:
    ...
```

```python
async def apply_policy_decision(
    self,
    user_id: UserId,
    candidate: MemoryCandidate,
    decision: MemoryPolicyDecision,
) -> MemoryWriteResult:
    ...
```

```python
async def delete_for_user(
    self,
    user_id: UserId,
    memory_id: MemoryId,
) -> None:
    ...
```

消费的数据类型：

```text
UserId
MemoryId
MemoryCandidate
MemoryPolicyDecision
str
int
```

返回的数据类型：

```text
list[LongTermMemory]
LongTermMemory
MemoryWriteResult
None
```

目的：

```text
持久化长期记忆，但只执行 MemoryPolicy 已允许的操作。
```

关键规则：

```text
MemoryRepository 不决定候选是否应该写入
写入前必须有 MemoryPolicyDecision
```

## 3. C：LLM Client / Real Agents / Prompts 负责人

### C 的目的

```text
让模型稳定地产生符合 schemas 的结构化输出。
```

C 不直接写数据库，不直接改 SessionState。

## C1. LLMClient

负责文件：

```text
llm/client.py
llm/model_registry.py
llm/retry_policy.py
llm/structured_output.py
llm/exceptions.py
tests/unit/test_llm_client.py
```

建议实现的函数：

```python
async def generate_text(
    self,
    prompt: str,
    model_name: str,
    temperature: float = 0.0,
) -> str:
    ...
```

```python
async def generate_structured(
    self,
    prompt: str,
    output_type: type[OutputT],
    model_name: str,
    temperature: float = 0.0,
) -> OutputT:
    ...
```

消费的数据类型：

```text
str
type[OutputT]
float
```

返回的数据类型：

```text
str
OutputT
```

目的：

```text
统一模型调用、重试、超时、结构化输出校验。
```

## C2. RiskAgent

负责文件：

```text
agents/risk_agent.py
prompts/risk_agent.md
tests/contract/test_risk_agent.py
```

需要保留：

```text
FakeRiskAgent
```

新增类：

```python
class RiskAgent:
    async def analyze(
        self,
        payload: RiskInput,
    ) -> RiskResult:
        ...
```

消费的数据类型：

```text
RiskInput
```

返回的数据类型：

```text
RiskResult
```

目的：

```text
判断本轮是否能走普通对话，还是必须进入安全流程。
```

fallback 规则：

```text
模型失败或输出非法时，route 应该保守地进入 safety_clarification
```

## C3. StateTracker

负责文件：

```text
agents/state_tracker.py
prompts/state_tracker.md
tests/contract/test_state_tracker.py
```

需要保留：

```text
FakeStateTracker
```

新增类：

```python
class StateTracker:
    async def extract_delta(
        self,
        payload: StateTrackerInput,
    ) -> StateDelta:
        ...
```

消费的数据类型：

```text
StateTrackerInput
```

返回的数据类型：

```text
StateDelta
```

目的：

```text
从当前用户消息里抽取状态变化，但不直接修改完整状态。
```

关键规则：

```text
明确用户陈述进入 topic_updates / goal_updates / reported_emotions
用户纠正进入 user_corrections
模型猜测只能进入 hypotheses
```

## C4. StrategyPlanner

负责文件：

```text
agents/strategy_planner.py
prompts/strategy_planner.md
tests/contract/test_strategy_planner.py
```

需要保留：

```text
FakeStrategyPlanner
```

新增类：

```python
class StrategyPlanner:
    async def plan(
        self,
        payload: StrategyPlannerInput,
    ) -> StrategyPlan:
        ...
```

消费的数据类型：

```text
StrategyPlannerInput
```

返回的数据类型：

```text
StrategyPlan
```

目的：

```text
选择下一轮对话策略，而不是生成最终回复。
```

关键规则：

```text
必须消费 FeedbackResult
不要重复用户明确拒绝过的策略
primary_strategy 必须是 StrategyType 中的一个
```

## C5. ResponseAgent

负责文件：

```text
agents/response_agent.py
prompts/response_agent.md
tests/contract/test_response_agent.py
```

需要保留：

```text
FakeResponseAgent
```

新增类：

```python
class ResponseAgent:
    async def generate(
        self,
        context: ResponseContext,
    ) -> DraftResponse:
        ...
```

消费的数据类型：

```text
ResponseContext
```

返回的数据类型：

```text
DraftResponse
```

目的：

```text
根据 context 和 strategy 生成候选回复草稿。
```

关键规则：

```text
不直接发给用户
不修改 state
不写 memory
```

## C6. OutputGuard

负责文件：

```text
agents/output_guard.py
prompts/output_guard.md
tests/contract/test_output_guard.py
```

需要保留：

```text
FakeOutputGuard
```

新增类：

```python
class OutputGuard:
    async def review(
        self,
        payload: GuardInput,
    ) -> GuardResult:
        ...
```

消费的数据类型：

```text
GuardInput
```

返回的数据类型：

```text
GuardResult
```

目的：

```text
在发送给用户前检查回复是否越界或不安全。
```

必须拦截：

```text
诊断
药物建议
制造依赖
绕过安全流程
错误引用用户历史
绝对化判断
```

## 4. D：Adaptive Loop / Summary / Memory Loop 负责人

### D 的目的

```text
把当前最小 loop 扩展成完整的反馈、长对话总结、跨 session memory loop。
```

D 不直接改公共 schemas。如果发现不够用，找 A 做 contract change。

## D1. FeedbackEvaluator

负责文件：

```text
agents/feedback_evaluator.py
prompts/feedback_evaluator.md
tests/contract/test_feedback_evaluator.py
```

需要保留：

```text
FakeFeedbackEvaluator
```

新增类：

```python
class FeedbackEvaluator:
    async def evaluate(
        self,
        payload: FeedbackInput,
    ) -> FeedbackResult:
        ...
```

消费的数据类型：

```text
FeedbackInput
```

返回的数据类型：

```text
FeedbackResult
```

目的：

```text
判断当前用户消息对上一轮 assistant 策略的可观察反馈。
```

关键规则：

```text
不要判断治疗是否成功
不要把用户继续聊天自动当成 positive feedback
用户明确拒绝时 strategy_fit 应该是 poor
```

## D2. Adaptive loop E2E 测试

负责文件：

```text
tests/end_to_end/test_adaptive_loop.py
```

需要测试的链路：

```text
第一轮生成 StrategyPlan
创建 pending InterventionRecord
第二轮用户拒绝上一轮方式
FeedbackEvaluator 产生 FeedbackResult
StrategyPlanner 切换策略
创建新的 pending InterventionRecord
```

消费/检查的数据类型：

```text
InterventionRecord
FeedbackResult
StrategyPlan
ChatTurnResult
```

目的：

```text
证明系统真的会根据用户反馈调整下一轮策略。
```

## D3. RollingSummarizer

负责文件：

```text
agents/rolling_summarizer.py
prompts/rolling_summarizer.md
workers/summary_worker.py
tests/contract/test_rolling_summarizer.py
```

新增类：

```python
class RollingSummarizer:
    async def summarize(
        self,
        payload: RollingSummarizerInput,
    ) -> RollingSummary:
        ...
```

消费的数据类型：

```text
RollingSummarizerInput
```

返回的数据类型：

```text
RollingSummary
```

目的：

```text
把长 session 中较早的消息压缩成可追溯 summary。
```

关键规则：

```text
不能凭空添加用户没说过的事实
重要结论要有 source_message_ids
summary 不覆盖最新原始消息
```

## D4. SummaryWorker

负责文件：

```text
workers/summary_worker.py
```

建议函数：

```python
async def handle_summary_requested(
    event: RollingSummaryRequestedEvent,
) -> RollingSummary:
    ...
```

消费的数据类型：

```text
RollingSummaryRequestedEvent
```

返回的数据类型：

```text
RollingSummary
```

目的：

```text
在后台或延迟任务中更新 rolling summary。
```

## D5. SessionFinalizer

负责文件：

```text
agents/session_finalizer.py
prompts/session_finalizer.md
workers/session_close_worker.py
tests/contract/test_session_finalizer.py
```

新增类：

```python
class SessionFinalizer:
    async def finalize(
        self,
        payload: SessionFinalizerInput,
    ) -> SessionFinalizerResult:
        ...
```

消费的数据类型：

```text
SessionFinalizerInput
```

返回的数据类型：

```text
SessionFinalizerResult
```

目的：

```text
在一次 session 结束时生成结构化总结，为跨 session memory 做准备。
```

返回结果里包括：

```text
session_summary
unfinished_topics
action_items
candidate_memories
strategy_outcomes
risk_events
source_message_ids
```

## D6. MemoryCurator

负责文件：

```text
agents/memory_curator.py
prompts/memory_curator.md
tests/contract/test_memory_curator.py
```

新增类：

```python
class MemoryCurator:
    async def curate(
        self,
        payload: SessionFinalizerResult,
    ) -> list[MemoryCandidate]:
        ...
```

或者如果需要更多上下文：

```python
class MemoryCurator:
    async def curate(
        self,
        payload: SessionFinalizerInput,
    ) -> list[MemoryCandidate]:
        ...
```

二选一即可，最终由 A 确认。

消费的数据类型：

```text
SessionFinalizerResult
或 SessionFinalizerInput
```

返回的数据类型：

```text
list[MemoryCandidate]
```

目的：

```text
从 session 中提出候选长期记忆，但不直接写入。
```

关键规则：

```text
MemoryCandidate 只是候选
不能直接写 LongTermMemory
敏感内容要标记 sensitivity
低置信度要降低 confidence 或要求用户确认
```

## D7. MemoryPolicy

负责文件：

```text
services/memory_policy.py
tests/unit/test_memory_policy.py
```

新增类：

```python
class MemoryPolicy:
    def evaluate_candidate(
        self,
        payload: MemoryPolicyInput,
    ) -> MemoryPolicyDecision:
        ...
```

消费的数据类型：

```text
MemoryPolicyInput
```

返回的数据类型：

```text
MemoryPolicyDecision
```

目的：

```text
决定候选记忆是否允许写入、强化、替代、冲突标记或拒绝。
```

关键规则：

```text
用户未开启 memory 时 allowed = False
敏感信息默认更严格
单次互动不能直接形成稳定 procedural memory
模型推断不能当成用户事实直接写入
```

## D8. ConflictResolver

负责文件：

```text
services/conflict_resolver.py
tests/unit/test_conflict_resolver.py
```

建议函数：

```python
def detect_memory_conflicts(
    candidate: MemoryCandidate,
    existing_memories: list[LongTermMemory],
) -> list[MemoryConflict]:
    ...
```

消费的数据类型：

```text
MemoryCandidate
list[LongTermMemory]
```

返回的数据类型：

```text
list[MemoryConflict]
```

目的：

```text
发现新旧长期记忆之间的冲突、重复或替代关系。
```

## D9. MemoryWorker

负责文件：

```text
workers/memory_worker.py
```

建议函数：

```python
async def handle_memory_candidate_created(
    event: MemoryCandidateCreatedEvent,
) -> list[MemoryWriteResult]:
    ...
```

消费的数据类型：

```text
MemoryCandidateCreatedEvent
MemoryCandidate
MemoryPolicyInput
MemoryPolicyDecision
```

返回的数据类型：

```text
list[MemoryWriteResult]
```

目的：

```text
把候选记忆交给 MemoryPolicy，再把允许的结果交给 MemoryRepository 执行。
```

## D10. Loop Evaluation

负责文件：

```text
evaluation/run_eval.py
evaluation/evaluators/strategy_evaluator.py
evaluation/evaluators/summary_evaluator.py
evaluation/evaluators/memory_evaluator.py
evaluation/evaluators/safety_evaluator.py
```

建议函数：

```python
def evaluate_strategy_switching(...) -> dict[str, int]:
    ...
```

```python
def evaluate_summary_faithfulness(...) -> dict[str, int]:
    ...
```

```python
def evaluate_memory_consistency(...) -> dict[str, int]:
    ...
```

目的：

```text
验证 adaptive loop、summary loop、memory loop 是否稳定。
```

## 5. 谁负责哪些数据结构

### A 主要负责

```text
SessionState
StateDelta
ResponseContext
ContextSection
ChatTurnResult
PostTurnEvent 接入位置
```

### B 主要负责

```text
Message
SessionState versions
InterventionRecord
RollingSummary
LongTermMemory
MemoryWriteResult
```

### C 主要负责

```text
RiskInput -> RiskResult
StateTrackerInput -> StateDelta
StrategyPlannerInput -> StrategyPlan
ResponseContext -> DraftResponse
GuardInput -> GuardResult
```

### D 主要负责

```text
FeedbackInput -> FeedbackResult
RollingSummarizerInput -> RollingSummary
SessionFinalizerInput -> SessionFinalizerResult
MemoryCandidate -> MemoryPolicyDecision
MemoryCandidateCreatedEvent -> MemoryWriteResult
```

## 6. 最重要的边界提醒

```text
A 管主流程和 context
B 管存储
C 管模型结构化输出
D 管 loop、summary、memory 语义
```

更具体地说：

```text
Context 管理：A
Memory 语义：D
Memory 存储：B
Memory 抽取用到的 LLM 能力：C 提供底层 client，D 写具体 loop agent
```

## 7. 给同事开工前的提醒

每个人开始前都先读：

```text
README.md
docs/contracts/README.md
docs/distributed_development_tasks.md
schemas/ 对应文件
```

每个人都不要自己发明新的 dict。

如果发现现有 schema 不够用，先找 A 讨论是否做 contract change。

