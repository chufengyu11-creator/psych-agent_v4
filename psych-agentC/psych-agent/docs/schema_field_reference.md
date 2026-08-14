# Schema 字段级数据类型说明

> 这份文档把当前 `schemas/` 里的抽象类型展开成更底层的数据形状。
>
> 读法：
>
> - Python 类型用于写代码和 mypy。
> - JSON 形状用于理解序列化后是什么样。
> - `list[T]` 在 JSON 里就是 array。
> - Pydantic model 在 JSON 里就是 object。
> - `str | None` 在 JSON 里就是 string 或 null。

## 0. 基础类型对照表

| Python / Pydantic 类型 | JSON 形状 | 说明 |
|---|---|---|
| `str` | string | 普通字符串 |
| `int` | number integer | 整数 |
| `float` | number | 小数或整数，常用于 confidence |
| `bool` | boolean | true / false |
| `datetime` | string | ISO 8601 时间字符串 |
| `list[T]` | array | 数组，里面每项是 T |
| `Model` | object | Pydantic model，序列化后是 JSON object |
| `A | None` | A 或 null | 可选字段 |
| `StrEnum` | string | 枚举，本质是受限字符串 |
| `UserId` | string | `typing.NewType`，运行时仍是 str |
| `SessionId` | string | `typing.NewType`，运行时仍是 str |
| `MessageId` | string | `typing.NewType`，运行时仍是 str |
| `MemoryId` | string | `typing.NewType`，运行时仍是 str |
| `InterventionId` | string | `typing.NewType`，运行时仍是 str |

## 1. ID 和证据来源

文件：`schemas/common.py`

### UserId / SessionId / MessageId / MemoryId / InterventionId

这些在代码里是不同类型，但 JSON 里都是 string。

```python
UserId = NewType("UserId", str)
SessionId = NewType("SessionId", str)
MessageId = NewType("MessageId", str)
MemoryId = NewType("MemoryId", str)
InterventionId = NewType("InterventionId", str)
```

JSON 示例：

```json
{
  "user_id": "user_123",
  "session_id": "session_456",
  "message_id": "msg_001"
}
```

### SourceReference

表示某个状态、summary、memory 来自哪条原始消息。

| 字段 | Python 类型 | JSON 形状 | 说明 |
|---|---|---|---|
| `message_id` | `MessageId` | string | 来源消息 ID |
| `quote` | `str | None` | string 或 null | 可选短引用 |

JSON 示例：

```json
{
  "message_id": "msg_001",
  "quote": "我最近很焦虑"
}
```

## 2. Message

文件：`schemas/messages.py`

### MessageRole

枚举，JSON 里是 string：

```text
user
assistant
system
```

### Message

| 字段 | Python 类型 | JSON 形状 | 说明 |
|---|---|---|---|
| `id` | `MessageId` | string | 消息 ID |
| `session_id` | `SessionId` | string | 所属 session |
| `role` | `MessageRole` | string enum | user / assistant / system |
| `content` | `str` | string | 原始消息内容 |
| `sequence_number` | `int` | integer | session 内第几条消息 |
| `created_at` | `datetime` | ISO datetime string | 创建时间 |
| `model_name` | `str | None` | string 或 null | assistant 消息可记录模型名 |
| `parent_message_id` | `MessageId | None` | string 或 null | 可选父消息 ID |

JSON 示例：

```json
{
  "id": "msg_001",
  "session_id": "session_123",
  "role": "user",
  "content": "我今天有点焦虑",
  "sequence_number": 1,
  "created_at": "2026-07-13T09:00:00Z",
  "model_name": null,
  "parent_message_id": null
}
```

### ChatTurnResult

| 字段 | Python 类型 | JSON 形状 | 说明 |
|---|---|---|---|
| `message_id` | `MessageId` | string | assistant 回复消息 ID |
| `response` | `str` | string | 最终发给用户的文本 |
| `session_id` | `SessionId` | string | session ID |
| `status` | `str` | string | 默认 `ok` |
| `state_version` | `int` | integer | 本轮后的 state version |

## 3. Risk

文件：`schemas/risk.py`

### RiskLevel

JSON string enum：

```text
low
medium
high
imminent
```

### RiskRoute

JSON string enum：

```text
normal_dialogue
safety_clarification
crisis_protocol
human_escalation
```

### RiskInput

| 字段 | Python 类型 | JSON 形状 | 说明 |
|---|---|---|---|
| `current_message` | `Message` | object | 当前用户消息 |
| `recent_messages` | `list[Message]` | array[object] | 最近消息数组 |
| `current_risk_level` | `RiskLevel` | string enum | 当前已有风险等级 |

### RiskResult

| 字段 | Python 类型 | JSON 形状 | 说明 |
|---|---|---|---|
| `risk_level` | `RiskLevel` | string enum | 本轮风险等级 |
| `categories` | `list[str]` | array[string] | 风险类别 |
| `needs_clarification` | `bool` | boolean | 是否需要安全澄清 |
| `route` | `RiskRoute` | string enum | 路由决定 |
| `reason_codes` | `list[str]` | array[string] | 原因代码 |
| `confidence` | `float` | number | 0 到 1 |

JSON 示例：

```json
{
  "risk_level": "low",
  "categories": [],
  "needs_clarification": false,
  "route": "normal_dialogue",
  "reason_codes": [],
  "confidence": 0.82
}
```

## 4. State

文件：`schemas/state.py`

### StateOperation

JSON string enum：

```text
add
update
remove
resolve_conflict
```

### ConversationPhase

JSON string enum：

```text
opening
exploration
goal_alignment
intervention
progress_check
closing
safety_check
crisis_protocol
human_escalation
```

### StateItem

| 字段 | Python 类型 | JSON 形状 | 说明 |
|---|---|---|---|
| `value` | `str` | string | 状态内容 |
| `source` | `SourceReference` | object | 来源证据 |
| `active` | `bool` | boolean | 是否仍有效 |
| `conflict_with` | `list[str]` | array[string] | 冲突项 ID 或描述 |

JSON 示例：

```json
{
  "value": "焦虑",
  "source": {
    "message_id": "msg_001",
    "quote": "我今天有点焦虑"
  },
  "active": true,
  "conflict_with": []
}
```

### TopicUpdate

| 字段 | Python 类型 | JSON 形状 |
|---|---|---|
| `operation` | `StateOperation` | string enum |
| `topic` | `str` | string |
| `source_message_id` | `MessageId` | string |

### GoalUpdate

| 字段 | Python 类型 | JSON 形状 |
|---|---|---|
| `operation` | `StateOperation` | string enum |
| `goal` | `str` | string |
| `source_message_id` | `MessageId` | string |

### EmotionReport

| 字段 | Python 类型 | JSON 形状 |
|---|---|---|
| `label` | `str` | string |
| `source_message_id` | `MessageId` | string |

### UserCorrection

| 字段 | Python 类型 | JSON 形状 | 说明 |
|---|---|---|---|
| `correction` | `str` | string | 用户纠正内容 |
| `replaces` | `str | None` | string 或 null | 被替代的旧内容 |
| `source_message_id` | `MessageId` | string | 来源消息 |

### StrategyPreference

| 字段 | Python 类型 | JSON 形状 |
|---|---|---|
| `operation` | `StateOperation` | string enum |
| `value` | `str` | string |
| `source_message_id` | `MessageId` | string |

### Hypothesis

模型假设，不是用户事实。

| 字段 | Python 类型 | JSON 形状 |
|---|---|---|
| `value` | `str` | string |
| `source_message_id` | `MessageId` | string |
| `confidence` | `float` | number |

### RiskState

| 字段 | Python 类型 | JSON 形状 |
|---|---|---|
| `level` | `RiskLevel` | string enum |
| `categories` | `list[str]` | array[string] |
| `reason_codes` | `list[str]` | array[string] |

### SessionState

`SessionState` 是嵌套结构。JSON 里是 object，里面包含多个 array/object。

| 字段 | Python 类型 | JSON 形状 | 说明 |
|---|---|---|---|
| `session_id` | `SessionId` | string | session ID |
| `version` | `int` | integer | 状态版本 |
| `phase` | `ConversationPhase` | string enum | 当前会话阶段 |
| `session_goal` | `str | None` | string 或 null | 当前目标 |
| `active_topics` | `list[StateItem]` | array[object] | 当前话题 |
| `reported_emotions` | `list[StateItem]` | array[object] | 用户明确表达的情绪 |
| `user_preferences` | `list[StateItem]` | array[object] | 用户沟通偏好 |
| `open_questions` | `list[StateItem]` | array[object] | 未解决问题 |
| `pending_action_plan` | `str | None` | string 或 null | 待定行动计划 |
| `risk_state` | `RiskState` | object | 当前风险状态 |

JSON 简化示例：

```json
{
  "session_id": "session_123",
  "version": 2,
  "phase": "intervention",
  "session_goal": "帮助用户选择一个低压力沟通步骤",
  "active_topics": [
    {
      "value": "工作沟通",
      "source": {"message_id": "msg_001", "quote": null},
      "active": true,
      "conflict_with": []
    }
  ],
  "reported_emotions": [],
  "user_preferences": [],
  "open_questions": [],
  "pending_action_plan": null,
  "risk_state": {
    "level": "low",
    "categories": [],
    "reason_codes": []
  }
}
```

### StateDelta

`StateDelta` 也是嵌套结构。它描述“这一轮发生了哪些变化”。

| 字段 | Python 类型 | JSON 形状 |
|---|---|---|
| `explicit_user_request` | `str | None` | string 或 null |
| `topic_updates` | `list[TopicUpdate]` | array[object] |
| `goal_updates` | `list[GoalUpdate]` | array[object] |
| `reported_emotions` | `list[EmotionReport]` | array[object] |
| `user_corrections` | `list[UserCorrection]` | array[object] |
| `strategy_preferences` | `list[StrategyPreference]` | array[object] |
| `hypotheses` | `list[Hypothesis]` | array[object] |

### StateTrackerInput

| 字段 | Python 类型 | JSON 形状 |
|---|---|---|
| `current_message` | `Message` | object |
| `previous_state` | `SessionState` | object |
| `recent_messages` | `list[Message]` | array[object] |
| `feedback` | `FeedbackResult | None` | object 或 null |

## 5. Feedback / Intervention

文件：`schemas/feedback.py`, `schemas/intervention.py`

### FeedbackLabel

JSON string enum：

```text
positive
negative
mixed
absent
```

### ObjectiveProgress

JSON string enum：

```text
achieved
partial
not_achieved
unknown
```

### StrategyFit

JSON string enum：

```text
good
mixed
poor
unknown
```

### InterventionRecord

| 字段 | Python 类型 | JSON 形状 |
|---|---|---|
| `intervention_id` | `InterventionId` | string |
| `session_id` | `SessionId` | string |
| `assistant_message_id` | `MessageId` | string |
| `strategy` | `str` | string |
| `objective` | `str` | string |
| `expected_signals` | `list[str]` | array[string] |
| `status` | `InterventionStatus` | string enum |
| `observed_response` | `str | None` | string 或 null |
| `explicit_feedback` | `str | None` | string 或 null |
| `strategy_fit` | `str | None` | string 或 null |
| `objective_progress` | `str | None` | string 或 null |
| `recommended_adjustment` | `str | None` | string 或 null |

### FeedbackInput

| 字段 | Python 类型 | JSON 形状 |
|---|---|---|
| `previous_intervention` | `InterventionRecord` | object |
| `assistant_message` | `Message` | object |
| `next_user_message` | `Message` | object |

### FeedbackResult

| 字段 | Python 类型 | JSON 形状 |
|---|---|---|
| `observed_response` | `str` | string |
| `explicit_feedback` | `FeedbackLabel` | string enum |
| `objective_progress` | `ObjectiveProgress` | string enum |
| `strategy_fit` | `StrategyFit` | string enum |
| `recommended_adjustment` | `str | None` | string 或 null |
| `confidence` | `float` | number |

## 6. Strategy

文件：`schemas/strategy.py`

### StrategyType

JSON string enum：

```text
reflective_listening
clarification
emotional_exploration
summarization
psychoeducation
collaborative_problem_solving
action_planning
progress_check
safety_check
session_closing
```

### StrategyPlannerInput

| 字段 | Python 类型 | JSON 形状 |
|---|---|---|
| `session_state` | `SessionState` | object |
| `risk` | `RiskResult` | object |
| `feedback` | `FeedbackResult | None` | object 或 null |
| `memories` | `RetrievedMemories` | object |

### StrategyPlan

| 字段 | Python 类型 | JSON 形状 |
|---|---|---|
| `conversation_phase` | `ConversationPhase` | string enum |
| `primary_strategy` | `StrategyType` | string enum |
| `objective` | `str` | string |
| `reason` | `str` | string |
| `avoid` | `list[str]` | array[string] |
| `expected_signals` | `list[str]` | array[string] |
| `switch_conditions` | `list[str]` | array[string] |

## 7. Memory

文件：`schemas/memory.py`

### MemoryType

JSON string enum：

```text
interaction_preference
active_goal
unfinished_topic
semantic_memory
episodic_memory
strategy_outcome
```

### MemorySensitivity

JSON string enum：

```text
low
medium
high
```

### MemorySourceType

JSON string enum：

```text
explicit_user_statement
session_summary
repeated_observation
model_inference
```

### MemoryOperation

JSON string enum：

```text
CREATE
REINFORCE
SUPERSEDE
MARK_CONFLICT
EXPIRE
DELETE
```

### LongTermMemory

| 字段 | Python 类型 | JSON 形状 |
|---|---|---|
| `id` | `MemoryId` | string |
| `memory_type` | `MemoryType` | string enum |
| `content` | `str` | string |
| `sensitivity` | `MemorySensitivity` | string enum |
| `confidence` | `float` | number |
| `source` | `list[SourceReference]` | array[object] |

### RetrievedMemories

| 字段 | Python 类型 | JSON 形状 |
|---|---|---|
| `semantic_memories` | `list[LongTermMemory]` | array[object] |
| `episodic_memories` | `list[LongTermMemory]` | array[object] |
| `active_goals` | `list[str]` | array[string] |
| `interaction_preferences` | `list[str]` | array[string] |
| `previous_session_summary` | `str | None` | string 或 null |

### MemoryRetrieverInput

| 字段 | Python 类型 | JSON 形状 |
|---|---|---|
| `user_id` | `UserId` | string |
| `query` | `str` | string |
| `session_state` | `SessionState` | object |
| `limit` | `int` | integer |

### MemoryCandidate

| 字段 | Python 类型 | JSON 形状 |
|---|---|---|
| `candidate_type` | `MemoryType` | string enum |
| `content` | `str` | string |
| `source_message_ids` | `list[MessageId]` | array[string] |
| `source_type` | `MemorySourceType` | string enum |
| `confidence` | `float` | number |
| `requires_user_confirmation` | `bool` | boolean |
| `sensitivity` | `MemorySensitivity` | string enum |
| `recommended_operation` | `MemoryOperation` | string enum |

### MemoryPolicyInput

| 字段 | Python 类型 | JSON 形状 |
|---|---|---|
| `candidate` | `MemoryCandidate` | object |
| `existing_memories` | `list[LongTermMemory]` | array[object] |
| `user_memory_enabled` | `bool` | boolean |
| `session_id` | `SessionId | None` | string 或 null |

### MemoryPolicyDecision

| 字段 | Python 类型 | JSON 形状 |
|---|---|---|
| `allowed` | `bool` | boolean |
| `operation` | `MemoryOperation | None` | string enum 或 null |
| `reason_codes` | `list[str]` | array[string] |
| `requires_user_confirmation` | `bool` | boolean |
| `target_memory_id` | `MemoryId | None` | string 或 null |
| `sanitized_content` | `str | None` | string 或 null |

### MemoryConflict

| 字段 | Python 类型 | JSON 形状 |
|---|---|---|
| `candidate` | `MemoryCandidate` | object |
| `existing_memory_id` | `MemoryId` | string |
| `reason` | `str` | string |
| `recommended_operation` | `MemoryOperation` | string enum |

### MemoryWriteResult

| 字段 | Python 类型 | JSON 形状 |
|---|---|---|
| `memory_id` | `MemoryId | None` | string 或 null |
| `operation` | `MemoryOperation` | string enum |
| `applied` | `bool` | boolean |
| `reason_codes` | `list[str]` | array[string] |

## 8. Context / Response / Guard

文件：`schemas/context.py`, `schemas/safety.py`

### ContextSection

| 字段 | Python 类型 | JSON 形状 |
|---|---|---|
| `name` | `str` | string |
| `content` | `str` | string |
| `token_budget` | `int` | integer |

### ResponseContext

| 字段 | Python 类型 | JSON 形状 |
|---|---|---|
| `system_policy` | `str` | string |
| `risk` | `RiskResult` | object |
| `session_state` | `SessionState` | object |
| `strategy` | `StrategyPlan` | object |
| `recent_messages` | `list[Message]` | array[object] |
| `rolling_summary` | `RollingSummary | None` | object 或 null |
| `memories` | `RetrievedMemories` | object |
| `sections` | `list[ContextSection]` | array[object] |

### DraftResponse

| 字段 | Python 类型 | JSON 形状 |
|---|---|---|
| `text` | `str` | string |
| `asked_question` | `bool` | boolean |
| `contains_action_suggestion` | `bool` | boolean |
| `referenced_memory_ids` | `list[str]` | array[string] |

### GuardInput

| 字段 | Python 类型 | JSON 形状 |
|---|---|---|
| `draft` | `DraftResponse` | object |
| `context` | `ResponseContext` | object |
| `risk` | `RiskResult` | object |

### GuardResult

| 字段 | Python 类型 | JSON 形状 |
|---|---|---|
| `decision` | `GuardDecision` | string enum |
| `violations` | `list[str]` | array[string] |
| `rewritten_response` | `str | None` | string 或 null |

## 9. Summary / Finalizer

文件：`schemas/summary.py`

### RollingSummary

| 字段 | Python 类型 | JSON 形状 |
|---|---|---|
| `session_id` | `SessionId` | string |
| `summary_version` | `int` | integer |
| `covered_from` | `MessageId` | string |
| `covered_to` | `MessageId` | string |
| `current_problem` | `str | None` | string 或 null |
| `session_goal` | `str | None` | string 或 null |
| `important_user_statements` | `list[str]` | array[string] |
| `strategies_attempted` | `list[str]` | array[string] |
| `strategy_responses` | `list[str]` | array[string] |
| `open_questions` | `list[str]` | array[string] |
| `source_message_ids` | `list[MessageId]` | array[string] |

### RollingSummarizerInput

| 字段 | Python 类型 | JSON 形状 |
|---|---|---|
| `session_id` | `SessionId` | string |
| `previous_summary` | `RollingSummary | None` | object 或 null |
| `uncovered_messages` | `list[Message]` | array[object] |
| `current_state` | `SessionState` | object |
| `interventions` | `list[InterventionRecord]` | array[object] |

### ActionItem

| 字段 | Python 类型 | JSON 形状 |
|---|---|---|
| `content` | `str` | string |
| `source_message_ids` | `list[MessageId]` | array[string] |
| `completed` | `bool` | boolean |

### StrategyOutcome

| 字段 | Python 类型 | JSON 形状 |
|---|---|---|
| `strategy` | `str` | string |
| `outcome` | `str` | string |
| `source_message_ids` | `list[MessageId]` | array[string] |

### SessionFinalizerInput

| 字段 | Python 类型 | JSON 形状 |
|---|---|---|
| `session_id` | `SessionId` | string |
| `messages` | `list[Message]` | array[object] |
| `final_state` | `SessionState` | object |
| `interventions` | `list[InterventionRecord]` | array[object] |
| `rolling_summary` | `RollingSummary | None` | object 或 null |

### SessionFinalizerResult

| 字段 | Python 类型 | JSON 形状 |
|---|---|---|
| `session_id` | `SessionId` | string |
| `session_summary` | `str` | string |
| `goal_updates` | `list[str]` | array[string] |
| `unfinished_topics` | `list[str]` | array[string] |
| `action_items` | `list[ActionItem]` | array[object] |
| `candidate_memories` | `list[MemoryCandidate]` | array[object] |
| `strategy_outcomes` | `list[StrategyOutcome]` | array[object] |
| `risk_events` | `list[str]` | array[string] |
| `source_message_ids` | `list[MessageId]` | array[string] |

## 10. Events

文件：`schemas/events.py`

### EventType

JSON string enum：

```text
post_turn
rolling_summary_requested
session_close_requested
memory_candidate_created
```

### BaseEvent

| 字段 | Python 类型 | JSON 形状 |
|---|---|---|
| `event_type` | `EventType` | string enum |
| `session_id` | `SessionId` | string |
| `created_at` | `datetime` | ISO datetime string |

### PostTurnEvent

| 字段 | Python 类型 | JSON 形状 |
|---|---|---|
| `event_type` | `EventType` | string enum |
| `session_id` | `SessionId` | string |
| `created_at` | `datetime` | ISO datetime string |
| `user_id` | `UserId` | string |
| `user_message_id` | `MessageId` | string |
| `assistant_message_id` | `MessageId` | string |
| `state_version` | `int` | integer |

### RollingSummaryRequestedEvent

| 字段 | Python 类型 | JSON 形状 |
|---|---|---|
| `event_type` | `EventType` | string enum |
| `session_id` | `SessionId` | string |
| `created_at` | `datetime` | ISO datetime string |
| `trigger` | `str` | string |
| `after_message_id` | `MessageId | None` | string 或 null |

### SessionCloseRequestedEvent

| 字段 | Python 类型 | JSON 形状 |
|---|---|---|
| `event_type` | `EventType` | string enum |
| `session_id` | `SessionId` | string |
| `created_at` | `datetime` | ISO datetime string |
| `user_id` | `UserId` | string |
| `reason` | `str | None` | string 或 null |

### MemoryCandidateCreatedEvent

| 字段 | Python 类型 | JSON 形状 |
|---|---|---|
| `event_type` | `EventType` | string enum |
| `session_id` | `SessionId` | string |
| `created_at` | `datetime` | ISO datetime string |
| `user_id` | `UserId` | string |
| `candidate_count` | `int` | integer |
| `source_message_ids` | `list[MessageId]` | array[string] |

## 11. 一句话理解嵌套层级

如果看到：

```text
SessionState
```

实际 JSON 是：

```text
object {
  string session_id,
  integer version,
  string phase,
  string|null session_goal,
  array[StateItem] active_topics,
  array[StateItem] reported_emotions,
  array[StateItem] user_preferences,
  array[StateItem] open_questions,
  string|null pending_action_plan,
  object RiskState
}
```

如果看到：

```text
ResponseContext
```

实际 JSON 是：

```text
object {
  string system_policy,
  object RiskResult,
  object SessionState,
  object StrategyPlan,
  array[Message] recent_messages,
  object|null RollingSummary,
  object RetrievedMemories,
  array[ContextSection] sections
}
```

如果看到：

```text
SessionFinalizerResult
```

实际 JSON 是：

```text
object {
  string session_id,
  string session_summary,
  array[string] goal_updates,
  array[string] unfinished_topics,
  array[ActionItem] action_items,
  array[MemoryCandidate] candidate_memories,
  array[StrategyOutcome] strategy_outcomes,
  array[string] risk_events,
  array[string] source_message_ids
}
```
