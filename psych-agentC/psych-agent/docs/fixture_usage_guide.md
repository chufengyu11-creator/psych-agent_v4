# 假数据使用说明

> 这份文档说明当前仓库里新增的标准假数据是什么、放在哪里、每类假数据用于测试什么，以及同事开发时应该怎么使用。

## 0. 为什么需要假数据

现在项目已经有了公共 schema 和最小主链路，但真实数据库、真实 LLM、summary loop、memory loop 还没有完全实现。

如果每个同事都自己临时编一些输入，很容易出现这些问题：

```text
每个人测的场景不一样
返回结构不一致
集成时才发现字段对不上
summary/memory/feedback 的预期不清楚
```

所以我们先准备一批标准假数据。

这批假数据的作用是：

```text
给单元测试用
给集成测试用
给 evaluation runner 用
给同事手动检查模块行为用
作为讨论数据结构和预期行为的共同样例
```

## 1. 这批假数据覆盖什么场景

核心多轮场景是：

```text
用户和直属领导沟通紧张
-> 用户开会前焦虑
-> assistant 先使用 reflective listening
-> 用户拒绝继续分析情绪
-> 用户要求具体下一步
-> assistant 切换到低压力行动建议
-> 用户表达偏好：不要一次太多建议，每次一个小步骤
```

这个场景可以同时测试：

```text
State tracking
Feedback loop
Strategy switching
Rolling summary
Session finalizer
Memory candidate
Memory policy
Context builder
```

另外还准备了 safety 场景：

```text
用户表达想伤害自己
```

用于测试：

```text
RiskAgent
SafetyRouter
OutputGuard
```

## 2. 文件位置

### 2.1 Python fixtures

目录：

```text
tests/fixtures/
```

文件：

```text
tests/fixtures/messages.py
tests/fixtures/states.py
tests/fixtures/interventions.py
tests/fixtures/memories.py
tests/fixtures/summaries.py
```

这些文件返回的是 Pydantic schema 对象，不是普通 dict。

例如：

```text
Message
SessionState
InterventionRecord
MemoryCandidate
RetrievedMemories
RollingSummary
SessionFinalizerInput
SessionFinalizerResult
```

### 2.2 JSON evaluation datasets

目录：

```text
evaluation/datasets/
```

文件：

```text
evaluation/datasets/adaptive_loop_cases.json
evaluation/datasets/summary_cases.json
evaluation/datasets/memory_cases.json
evaluation/datasets/safety_cases.json
evaluation/datasets/README.md
```

这些 JSON 文件不是运行时 schema，而是 evaluation case。

它们描述：

```text
输入场景
期望结果
评测要检查哪些字段
```

## 3. Python fixtures 怎么用

### 3.1 多轮消息 fixture

文件：

```text
tests/fixtures/messages.py
```

主要函数：

```python
work_stress_dialogue() -> list[Message]
safety_check_dialogue() -> list[Message]
make_message(...) -> Message
```

示例：

```python
from tests.fixtures.messages import work_stress_dialogue

messages = work_stress_dialogue()

assert messages[0].content == "我最近和直属领导沟通很紧张，每次开会前都会焦虑。"
assert messages[1].role == "assistant"
```

适合谁用：

```text
B：测试 MessageRepository
C：测试 RiskAgent / StateTracker
D：测试 RollingSummarizer / SessionFinalizer / FeedbackEvaluator
```

### 3.2 SessionState fixture

文件：

```text
tests/fixtures/states.py
```

主要函数：

```python
empty_state() -> SessionState
work_stress_state() -> SessionState
```

示例：

```python
from tests.fixtures.states import work_stress_state

state = work_stress_state()

assert state.session_goal == "帮助用户选择一个低压力的沟通步骤"
assert state.reported_emotions[0].value == "焦虑"
```

适合谁用：

```text
A：测试 ContextBuilder / StateReducer
B：测试 StateRepository
C：测试 StrategyPlanner
D：测试 SessionFinalizer / Memory loop
```

### 3.3 Intervention fixture

文件：

```text
tests/fixtures/interventions.py
```

主要函数：

```python
pending_reflective_intervention() -> InterventionRecord
evaluated_poor_fit_intervention() -> InterventionRecord
```

示例：

```python
from tests.fixtures.interventions import pending_reflective_intervention

intervention = pending_reflective_intervention()

assert intervention.strategy == "reflective_listening"
assert intervention.status == "pending"
```

适合谁用：

```text
B：测试 InterventionRepository
C：测试 StrategyPlanner 是否读取上一轮策略
D：测试 FeedbackEvaluator 和 adaptive loop
```

### 3.4 Memory fixture

文件：

```text
tests/fixtures/memories.py
```

主要函数：

```python
interaction_preference_candidate() -> MemoryCandidate
active_goal_memory() -> LongTermMemory
retrieved_work_stress_memories() -> RetrievedMemories
memory_policy_input() -> MemoryPolicyInput
fixture_user_id() -> UserId
```

示例：

```python
from tests.fixtures.memories import interaction_preference_candidate

candidate = interaction_preference_candidate()

assert candidate.candidate_type == "interaction_preference"
assert candidate.recommended_operation == "CREATE"
```

适合谁用：

```text
A：测试 ContextBuilder 如何使用 RetrievedMemories
B：测试 MemoryRepository
D：测试 MemoryCurator / MemoryPolicy / ConflictResolver
```

### 3.5 Summary / Finalizer fixture

文件：

```text
tests/fixtures/summaries.py
```

主要函数：

```python
work_stress_rolling_summary() -> RollingSummary
rolling_summarizer_input() -> RollingSummarizerInput
session_finalizer_input() -> SessionFinalizerInput
session_finalizer_result() -> SessionFinalizerResult
```

示例：

```python
from tests.fixtures.summaries import rolling_summarizer_input

payload = rolling_summarizer_input()

assert len(payload.uncovered_messages) == 5
assert payload.current_state.session_goal is not None
```

适合谁用：

```text
A：测试 ContextBuilder 接收 RollingSummary
B：测试 SummaryRepository
D：测试 RollingSummarizer / SessionFinalizer
```

## 4. JSON datasets 怎么用

JSON datasets 主要给 evaluation runner 或手动评测用。

读取示例：

```python
import json
from pathlib import Path

payload = json.loads(
    Path("evaluation/datasets/adaptive_loop_cases.json").read_text(encoding="utf-8")
)

for case in payload["cases"]:
    print(case["id"], case["description"])
```

### 4.1 adaptive_loop_cases.json

用途：测试 feedback loop 和 strategy switching。

包含场景：

```text
用户先表达工作压力
assistant 使用 reflective_listening
用户明确拒绝继续分析情绪
用户要求具体步骤
```

期望检查：

```text
FeedbackResult.explicit_feedback = negative
FeedbackResult.strategy_fit = poor
FeedbackResult.objective_progress = not_achieved
下一轮 strategy 切到 clarification / collaborative_problem_solving / action_planning
```

适合谁用：

```text
C：StrategyPlanner
D：FeedbackEvaluator / adaptive loop E2E
```

### 4.2 summary_cases.json

用途：测试 rolling summary 是否忠实。

期望检查：

```text
summary 覆盖 msg_001 到 msg_003
current_problem 包含 直属领导 / 焦虑
session_goal 包含 低压力 / 沟通
important_user_statements 包含 用户拒绝继续分析情绪和要求具体步骤
source_message_ids 包含 msg_001 / msg_003
```

适合谁用：

```text
D：RollingSummarizer / summary evaluator
B：SummaryRepository
A：ContextBuilder 如何接收 summary
```

### 4.3 memory_cases.json

用途：测试 memory candidate 和 memory policy。

包含两个场景：

```text
interaction_preference：用户希望一次只收到一个小步骤
unfinished_topic：用户希望下次继续聊和直属领导如何开口
```

期望检查：

```text
candidate_type
content_contains
source_message_ids
source_type
sensitivity
recommended_operation
requires_user_confirmation
```

适合谁用：

```text
D：MemoryCurator / MemoryPolicy / ConflictResolver
B：MemoryRepository
```

### 4.4 safety_cases.json

用途：测试风险识别和安全路由。

包含两个场景：

```text
自伤想法：应该进入安全路径
普通工作压力：不应该误判成危机路径
```

期望检查：

```text
RiskResult.risk_level
RiskResult.route
RiskResult.categories
```

适合谁用：

```text
C：RiskAgent
C：OutputGuard
A：SafetyRouter integration
```

## 5. 目前保护假数据的测试

新增测试：

```text
tests/unit/test_fixture_builders.py
tests/unit/test_evaluation_datasets.py
```

它们检查：

```text
Python fixtures 返回的是当前 schema 对象
多轮对话 fixture 有正确 turn 顺序
memory fixture 使用当前 MemoryType / MemoryOperation
summary fixture 使用当前 RollingSummary / SessionFinalizerResult
JSON datasets 能正常 parse
每个 JSON case 都有 id 和 description
```

## 6. 同事开发时怎么选择假数据

### 如果你在做 B：storage / repository

优先用：

```text
tests/fixtures/messages.py
tests/fixtures/states.py
tests/fixtures/interventions.py
tests/fixtures/memories.py
tests/fixtures/summaries.py
```

建议测试：

```text
MessageRepository 能保存 work_stress_dialogue()
StateRepository 能保存 work_stress_state()
InterventionRepository 能保存 pending_reflective_intervention()
MemoryRepository 能保存 interaction_preference_candidate()
SummaryRepository 能保存 work_stress_rolling_summary()
```

### 如果你在做 C：LLM agents

优先用：

```text
evaluation/datasets/safety_cases.json
evaluation/datasets/adaptive_loop_cases.json
tests/fixtures/messages.py
tests/fixtures/states.py
```

建议测试：

```text
RiskAgent 在 safety_cases 上 route 正确
StateTracker 能从 work_stress_dialogue 里抽取焦虑、工作沟通、具体建议偏好
StrategyPlanner 能根据 negative feedback 切策略
ResponseAgent 生成 DraftResponse
OutputGuard 返回 GuardResult
```

### 如果你在做 D：loop / summary / memory

优先用：

```text
evaluation/datasets/adaptive_loop_cases.json
evaluation/datasets/summary_cases.json
evaluation/datasets/memory_cases.json
tests/fixtures/interventions.py
tests/fixtures/summaries.py
tests/fixtures/memories.py
```

建议测试：

```text
FeedbackEvaluator 对拒绝上一轮策略的用户消息返回 poor fit
RollingSummarizer 生成的 summary 包含 source_message_ids
SessionFinalizer 生成 action_items 和 candidate_memories
MemoryCurator 生成 MemoryCandidate
MemoryPolicy 对低敏感明确偏好允许写入或不要求确认
```

### 如果你在做 A：context / integration

优先用：

```text
tests/fixtures/messages.py
tests/fixtures/states.py
tests/fixtures/memories.py
tests/fixtures/summaries.py
```

建议测试：

```text
ContextBuilder 能把 SessionState、RollingSummary、RetrievedMemories、recent messages 放进 ResponseContext
TurnOrchestrator 在多轮场景里能创建 pending intervention
local adapter 能返回 ChatTurnResult
```

## 7. 修改假数据时的规则

如果要改 fixture 或 JSON dataset，请遵守：

```text
不要删除已有 case，除非确认没有人依赖
新增 case 必须有稳定 id
新增 case 必须有 description
JSON 文件必须是 UTF-8 无 BOM
Python fixtures 必须返回 Pydantic schema 对象
改完必须跑 ruff check ., mypy ., pytest
```

## 8. 推荐下一步

现在可以让同事开始基于这些假数据写模块。

推荐顺序：

```text
1. B 用 fixtures 写 repository tests
2. C 用 safety/adaptive datasets 写 agent contract tests
3. D 用 adaptive/summary/memory datasets 写 loop tests
4. A 用 fixtures 写 ContextBuilder 和 local adapter tests
```
