# 数据结构与模块输入输出说明

> 这份文档用于团队讨论和分布式开发。
> 重点回答三个问题：
>
> 1. 每个模块消费什么数据结构？
> 2. 每个模块返回什么数据结构？
> 3. 返回结果会被谁继续消费？

## 0. 先说结论

当前系统已经有两个最小 loop：

```text
Loop 1：单轮状态循环
用户消息 -> StateDelta -> SessionState -> StrategyPlan -> Response

Loop 2：策略反馈循环
上一轮 InterventionRecord -> 当前用户反馈 -> FeedbackResult -> 下一轮 StrategyPlan
```

还没有完整实现两个更大的 loop：

```text
Loop 3：长对话 Rolling Summary Loop
Loop 4：跨 Session Memory Loop
```

所以现在不是没有 loop，而是：

```text
已有最小单轮 loop 和最小反馈 loop
summary loop / memory loop 还只是留好了接口和方向
```

## 1. 核心边界

团队里最容易混淆的是这四个词：

```text
Message
State
Context
Memory
```

它们不是同一个东西。

### Message

Message 是原始消息，是真实事实来源。

```text
用户说了什么
assistant 回了什么
什么时候说的
属于哪个 session
```

Message 应该 append-only，不要随便修改历史消息。

### State

State 是系统对当前 session 的结构化理解。

例如：

```text
当前话题
当前目标
用户明确表达的情绪
用户偏好的对话方式
当前风险状态
```

State 不是原始事实，而是从 Message 派生出来的结构化状态。

### Context

Context 是本轮临时喂给模型的输入。

Context 会从多个地方拼出来：

```text
system policy
risk state
session state
strategy plan
rolling summary
recent messages
retrieved memories
```

Context 不应该直接写数据库。

### Memory

Memory 是跨 session 仍然可能有用的信息。

例如：

```text
用户明确确认过的长期目标
用户明确表达的沟通偏好
上次没聊完的话题
多次验证有效或无效的支持方式
```

Memory 不能由模型直接永久写入，必须经过 MemoryPolicy。

## 2. 单轮主链路数据流

当前主链路是：

```text
用户输入
-> Message
-> FeedbackResult | None
-> RiskResult
-> StateDelta
-> SessionState
-> RetrievedMemories
-> StrategyPlan
-> ResponseContext
-> DraftResponse
-> GuardResult
-> ChatTurnResult
```

表格版：

| 模块 | 消费的数据结构 | 返回的数据结构 | 下游消费者 |
|---|---|---|---|
| 本地入口 / Orchestrator | `user_id`, `session_id`, `text` | `ChatTurnResult` | 本地部署系统 |
| `MessageRepository.create_user_message` | `SessionId`, `text` | `Message` | RiskAgent, StateTracker, history |
| `FeedbackEvaluator` | `FeedbackInput` | `FeedbackResult` | StateReducer, StrategyPlanner |
| `RiskAgent` | `RiskInput` | `RiskResult` | SafetyRouter, StateReducer, ContextBuilder, OutputGuard |
| `StateTracker` | `StateTrackerInput` | `StateDelta` | StateReducer |
| `StateReducer` | `SessionState`, `StateDelta`, `FeedbackResult | None`, `RiskResult` | 新版 `SessionState` | StateRepository, MemoryRetriever, StrategyPlanner, ContextBuilder |
| `MemoryRetriever` | `user_id`, `query`, `SessionState` | `RetrievedMemories` | StrategyPlanner, ContextBuilder |
| `StrategyPlanner` | `StrategyPlannerInput` | `StrategyPlan` | ContextBuilder, InterventionRepository |
| `ContextBuilder` | `SessionState`, `RollingSummary | None`, `list[Message]`, `RetrievedMemories`, `StrategyPlan`, `RiskResult` | `ResponseContext` | ResponseAgent, OutputGuard |
| `ResponseAgent` | `ResponseContext` | `DraftResponse` | OutputGuard |
| `OutputGuard` | `GuardInput` | `GuardResult` | Orchestrator |
| `MessageRepository.create_assistant_message` | `SessionId`, `final_text` | `Message` | InterventionRepository |
| `InterventionRepository.create_pending` | assistant message + strategy info | `InterventionRecord` | 下一轮 FeedbackEvaluator |

## 3. 当前已经实现的核心结构

### 3.1 Message

文件：

```text
schemas/messages.py
```

用途：保存原始对话消息。

```python
class Message:
    id: MessageId
    session_id: SessionId
    role: MessageRole
    content: str
    sequence_number: int
    created_at: datetime
    model_name: str | None
    parent_message_id: MessageId | None
```

谁生产：

```text
MessageRepository
```

谁消费：

```text
RiskAgent
StateTracker
FeedbackEvaluator
ContextBuilder
RollingSummarizer
MemoryCurator
AuditLogger
```

关键规则：

```text
Message 是原始事实来源
重要状态、summary、memory 都应该能追溯到 MessageId
```

### 3.2 RiskInput / RiskResult

文件：

```text
schemas/risk.py
```

用途：判断本轮是否可以正常对话，还是需要安全流程。

```python
class RiskInput:
    current_message: Message
    recent_messages: list[Message]
    current_risk_level: RiskLevel

class RiskResult:
    risk_level: RiskLevel
    categories: list[str]
    needs_clarification: bool
    route: RiskRoute
    reason_codes: list[str]
    confidence: float
```

谁生产：

```text
RiskAgent
```

谁消费：

```text
TurnOrchestrator
SafetyRouter
StateReducer
ContextBuilder
OutputGuard
AuditLogger
```

关键规则：

```text
RiskAgent 只负责判断风险和建议 route
RiskAgent 不负责生成普通回复
RiskAgent 不直接修改 SessionState
```

### 3.3 StateTrackerInput / StateDelta

文件：

```text
schemas/state.py
```

用途：从当前用户消息中提取“状态变化”，但不直接修改完整状态。

```python
class StateTrackerInput:
    current_message: Message
    previous_state: SessionState
    recent_messages: list[Message]
    feedback: FeedbackResult | None

class StateDelta:
    explicit_user_request: str | None
    topic_updates: list[TopicUpdate]
    goal_updates: list[GoalUpdate]
    reported_emotions: list[EmotionReport]
    user_corrections: list[UserCorrection]
    strategy_preferences: list[StrategyPreference]
    hypotheses: list[Hypothesis]
```

谁生产：

```text
StateTracker
```

谁消费：

```text
StateReducer
AuditLogger
Evaluation
```

关键规则：

```text
StateTracker 只能返回 StateDelta
不能返回完整 SessionState
不能写数据库
用户明确说的话和模型假设必须分开
```

### 3.4 SessionState

文件：

```text
schemas/state.py
```

用途：保存当前 session 的结构化状态。

```python
class SessionState:
    session_id: SessionId
    version: int
    phase: ConversationPhase
    session_goal: str | None
    active_topics: list[StateItem]
    reported_emotions: list[StateItem]
    user_preferences: list[StateItem]
    open_questions: list[StateItem]
    pending_action_plan: str | None
    risk_state: RiskState
```

谁生产：

```text
StateReducer
```

谁消费：

```text
StateRepository
MemoryRetriever
StrategyPlanner
ContextBuilder
SessionFinalizer
AuditLogger
```

关键规则：

```text
只有 StateReducer 可以把 StateDelta 合并成新的 SessionState
State 应该 versioned
不要原地修改旧 state
```

### 3.5 FeedbackInput / FeedbackResult

文件：

```text
schemas/feedback.py
```

用途：判断用户当前回复对上一轮 assistant 策略的反应。

```python
class FeedbackInput:
    previous_intervention: InterventionRecord
    assistant_message: Message
    next_user_message: Message

class FeedbackResult:
    observed_response: str
    explicit_feedback: FeedbackLabel
    objective_progress: ObjectiveProgress
    strategy_fit: StrategyFit
    recommended_adjustment: str | None
    confidence: float
```

谁生产：

```text
FeedbackEvaluator
```

谁消费：

```text
StateReducer
StrategyPlanner
InterventionRepository
Evaluation
```

关键规则：

```text
FeedbackEvaluator 只判断可观察反馈
不能判断“治疗成功”
不能把用户继续聊天自动当作 positive feedback
```

### 3.6 InterventionRecord

文件：

```text
schemas/intervention.py
```

用途：记录某一轮 assistant 使用了什么策略，等待下一轮用户反馈。

```python
class InterventionRecord:
    intervention_id: InterventionId
    session_id: SessionId
    assistant_message_id: MessageId
    strategy: str
    objective: str
    expected_signals: list[str]
    status: InterventionStatus
    observed_response: str | None
    explicit_feedback: str | None
    strategy_fit: str | None
    objective_progress: str | None
    recommended_adjustment: str | None
```

谁生产：

```text
InterventionRepository.create_pending
```

谁消费：

```text
FeedbackEvaluator
StrategyPlanner
SessionFinalizer
AuditLogger
Evaluation
```

关键规则：

```text
每轮 assistant 回复后创建 pending intervention
下一轮用户消息到来时评估这个 pending intervention
```

### 3.7 StrategyPlannerInput / StrategyPlan

文件：

```text
schemas/strategy.py
```

用途：决定下一轮使用什么对话策略。

```python
class StrategyPlannerInput:
    session_state: SessionState
    risk: RiskResult
    feedback: FeedbackResult | None
    memories: RetrievedMemories

class StrategyPlan:
    conversation_phase: ConversationPhase
    primary_strategy: StrategyType
    objective: str
    reason: str
    avoid: list[str]
    expected_signals: list[str]
    switch_conditions: list[str]
```

谁生产：

```text
StrategyPlanner
```

谁消费：

```text
ContextBuilder
ResponseAgent
InterventionRepository
AuditLogger
```

关键规则：

```text
StrategyPlanner 不直接生成最终回复
StrategyPlanner 应该消费 feedback，避免重复用户拒绝过的策略
```

### 3.8 RetrievedMemories / LongTermMemory

文件：

```text
schemas/memory.py
```

用途：MemoryRetriever 给当前轮找出来的相关长期信息。

```python
class LongTermMemory:
    id: MemoryId
    memory_type: str
    content: str
    sensitivity: str
    confidence: float
    source: list[SourceReference]

class RetrievedMemories:
    semantic_memories: list[LongTermMemory]
    episodic_memories: list[LongTermMemory]
    active_goals: list[str]
    interaction_preferences: list[str]
    previous_session_summary: str | None
```

谁生产：

```text
MemoryRetriever
```

谁消费：

```text
StrategyPlanner
ContextBuilder
ResponseAgent
```

关键规则：

```text
MemoryRetriever 只负责取相关 memory
它不决定是否创建新 memory
它不负责写 memory
```

### 3.9 ResponseContext / ContextSection

文件：

```text
schemas/context.py
```

用途：本轮真正喂给 ResponseAgent 的上下文。

```python
class ContextSection:
    name: str
    content: str
    token_budget: int

class ResponseContext:
    system_policy: str
    risk: RiskResult
    session_state: SessionState
    strategy: StrategyPlan
    recent_messages: list[Message]
    rolling_summary: RollingSummary | None
    memories: RetrievedMemories
    sections: list[ContextSection]
```

谁生产：

```text
ContextBuilder
```

谁消费：

```text
ResponseAgent
OutputGuard
AuditLogger
```

关键规则：

```text
Context 是临时输入，不是数据库状态
ContextBuilder 不写数据库
当前原话优先级高于旧 summary 和旧 memory
```

### 3.10 DraftResponse / GuardInput / GuardResult

文件：

```text
schemas/safety.py
```

用途：先生成草稿，再过安全检查。

```python
class DraftResponse:
    text: str
    asked_question: bool
    contains_action_suggestion: bool
    referenced_memory_ids: list[str]

class GuardInput:
    draft: DraftResponse
    context: ResponseContext
    risk: RiskResult

class GuardResult:
    decision: GuardDecision
    violations: list[str]
    rewritten_response: str | None
```

谁生产：

```text
DraftResponse: ResponseAgent
GuardResult: OutputGuard
```

谁消费：

```text
GuardInput: OutputGuard
GuardResult: TurnOrchestrator
```

关键规则：

```text
ResponseAgent 不直接发给用户
OutputGuard 决定 allow/rewrite/block/route_to_safety
```

### 3.11 RollingSummary

文件：

```text
schemas/summary.py
```

用途：压缩长 session 中较早的消息。

```python
class RollingSummary:
    session_id: SessionId
    summary_version: int
    covered_from: MessageId
    covered_to: MessageId
    current_problem: str | None
    session_goal: str | None
    important_user_statements: list[str]
    strategies_attempted: list[str]
    strategy_responses: list[str]
    open_questions: list[str]
    source_message_ids: list[MessageId]
```

谁生产：

```text
RollingSummarizer
SummaryWorker
```

谁消费：

```text
SummaryRepository
ContextBuilder
SessionFinalizer
Evaluation
```

关键规则：

```text
Summary 不能凭空添加用户没说过的事实
重要结论必须可追溯到 source message ids
最新原始消息优先于 summary
```

### 3.12 MemoryCandidate

文件：

```text
schemas/memory.py
```

用途：MemoryCurator 提出的候选长期记忆。

```python
class MemoryCandidate:
    candidate_type: str
    content: str
    source_message_ids: list[str]
    source_type: str
    confidence: float
    requires_user_confirmation: bool
    sensitivity: str
    recommended_operation: MemoryOperation
```

谁生产：

```text
MemoryCurator
SessionFinalizer
```

谁消费：

```text
MemoryPolicy
ConflictResolver
MemoryRepository
用户确认流程
```

关键规则：

```text
MemoryCandidate 只是候选，不等于已经写入长期记忆
长期记忆写入必须经过 MemoryPolicy
```

## 4. 当前已有 Loop

### Loop 1：单轮状态循环

```text
Message
-> StateTrackerInput
-> StateDelta
-> StateReducer
-> SessionState
-> StrategyPlannerInput
-> StrategyPlan
-> ResponseContext
-> DraftResponse
```

作用：让系统每轮都更新“当前理解”，并基于最新状态生成策略。

当前状态：

```text
已有最小实现
已有测试覆盖
```

### Loop 2：策略反馈循环

```text
上一轮 StrategyPlan
-> InterventionRecord pending
-> 下一轮用户 Message
-> FeedbackInput
-> FeedbackResult
-> StateReducer / StrategyPlanner
-> 新 StrategyPlan
```

作用：让系统知道上一轮策略有没有被用户接受，以及下一轮是否应该切换策略。

当前状态：

```text
已有最小实现
已有测试覆盖
```

## 5. 还需要补齐的 Loop

### Loop 3：长对话 Rolling Summary Loop

目标链路：

```text
recent messages 变多
-> RollingSummarizerInput
-> RollingSummary
-> SummaryRepository.save_version
-> ContextBuilder 读取 RollingSummary
-> ResponseContext
```

当前已有：

```text
RollingSummary schema
SummaryRepository.get_current
ContextBuilder 支持 rolling_summary
```

还缺：

```text
RollingSummarizerInput
RollingSummarizer 实现
summary_worker
summary 触发条件
summary faithfulness evaluation
```

建议新增结构：

```python
class RollingSummarizerInput:
    session_id: SessionId
    previous_summary: RollingSummary | None
    uncovered_messages: list[Message]
    current_state: SessionState
    interventions: list[InterventionRecord]
```

### Loop 4：跨 Session Memory Loop

目标链路：

```text
Session 结束
-> SessionFinalizerInput
-> SessionFinalizerResult
-> MemoryCandidate
-> MemoryPolicyDecision
-> MemoryRepository 写入
-> 下一次 session MemoryRetriever 检索
-> RetrievedMemories
-> ContextBuilder
```

当前已有：

```text
LongTermMemory
RetrievedMemories
MemoryCandidate
MemoryRetriever placeholder
ContextBuilder 支持 memories
```

还缺：

```text
SessionFinalizerInput
SessionFinalizerResult
MemoryPolicyInput
MemoryPolicyDecision
MemoryRepository
跨 session bootstrap
```

建议新增结构：

```python
class SessionFinalizerInput:
    session_id: SessionId
    messages: list[Message]
    final_state: SessionState
    interventions: list[InterventionRecord]
    rolling_summary: RollingSummary | None

class SessionFinalizerResult:
    session_summary: str
    goal_updates: list[str]
    unfinished_topics: list[str]
    action_items: list[str]
    candidate_memories: list[MemoryCandidate]
    strategy_outcomes: list[str]
    risk_events: list[str]
```

```python
class MemoryPolicyInput:
    candidate: MemoryCandidate
    existing_memories: list[LongTermMemory]
    user_memory_enabled: bool

class MemoryPolicyDecision:
    allowed: bool
    operation: MemoryOperation | None
    reason_codes: list[str]
    requires_user_confirmation: bool
    target_memory_id: MemoryId | None
```

## 6. 按负责人划分数据结构边界

### A 负责

A 负责主流程和 context 管理。

主要数据结构：

```text
SessionState
StateDelta
ResponseContext
ContextSection
ChatTurnResult
```

A 关注的问题：

```text
本轮应该给模型什么上下文
状态如何被合并
主流程是否稳定
各模块是否按 contract 交接
```

### B 负责

B 负责持久化。

主要数据结构：

```text
Message
SessionState versions
InterventionRecord
RollingSummary
LongTermMemory
```

B 关注的问题：

```text
怎么存
怎么查
怎么 version
怎么 append-only
怎么删除或失效
```

### C 负责

C 负责模型 agent 和结构化输出。

主要数据结构：

```text
RiskInput -> RiskResult
StateTrackerInput -> StateDelta
StrategyPlannerInput -> StrategyPlan
ResponseContext -> DraftResponse
GuardInput -> GuardResult
```

C 关注的问题：

```text
模型怎么稳定返回 Pydantic object
输出非法时怎么 fallback
prompt 如何和 schema 对齐
```

### D 负责

D 负责 loop、summary、memory 语义。

主要数据结构：

```text
FeedbackInput -> FeedbackResult
RollingSummarizerInput -> RollingSummary
SessionFinalizerInput -> SessionFinalizerResult
MemoryCandidate -> MemoryPolicyDecision
```

D 关注的问题：

```text
上一轮策略效果如何进入下一轮
长对话怎么总结
哪些信息值得成为长期记忆候选
哪些候选可以写入长期记忆
新旧记忆冲突怎么办
```

## 7. 当前最值得讨论的开放问题

### 问题 1：SessionState 是否还需要更细

当前 `SessionState` 比较轻。

可能需要进一步拆分：

```text
current_focus
user_goals
emotional_state
strategy_preferences
risk_state
open_questions
action_plan
```

### 问题 2：Memory 类型是否要固定枚举

当前 `memory_type: str` 比较灵活。

可以考虑改成：

```text
interaction_preference
active_goal
unfinished_topic
semantic_memory
episodic_memory
strategy_outcome
```

### 问题 3：SourceReference 是否足够

当前：

```python
message_id: MessageId
quote: str | None
```

可能后续需要：

```text
span_start
span_end
confidence
source_agent
```

### 问题 4：InterventionRecord.strategy 是否应该用 StrategyType

当前是：

```python
strategy: str
```

为了更强约束，可以考虑改成：

```python
strategy: StrategyType
```

但这属于 contract change，需要单独讨论。

### 问题 5：是否需要统一事件结构

后续 worker 可能需要事件：

```text
PostTurnEvent
SummaryRequestedEvent
SessionCloseEvent
MemoryCandidateCreatedEvent
```

如果要做异步 worker，建议补这些结构。

## 8. 建议下一步

建议先做一次小型 contract 讨论，只讨论这些问题：

```text
1. SessionState 当前字段够不够？
2. Memory 类型是否要枚举化？
3. 是否马上新增 RollingSummarizerInput？
4. 是否马上新增 SessionFinalizerResult？
5. 是否马上新增 MemoryPolicyDecision？
6. InterventionRecord.strategy 是否改成 StrategyType？
7. 本地部署入口是否新增 local_runtime/adapter.py？
```

讨论完以后，再让同事分别开始实现。

这样可以避免：

```text
D 写 memory loop 时发现缺 schema
B 写数据库时不知道 memory 怎么存
C 写 prompt 时输出结构和 schema 对不上
A 整合时发现大家返回的数据形状不同
```
