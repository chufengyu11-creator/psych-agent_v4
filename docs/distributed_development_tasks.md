# 分布式开发任务计划

> 目的：把当前已经搭好的 contract base 拆成可以并行开发的具体任务。
> 目标是让大家尽量隔离开发，减少冲突，最后由整合负责人统一接起来。

## 0. 当前状态

当前仓库已经有一个最小可运行底座：

```text
用户消息
-> TurnOrchestrator
-> FakeRiskAgent
-> FakeStateTracker
-> StateReducer
-> FakeStrategyPlanner
-> ContextBuilder
-> FakeResponseAgent
-> FakeOutputGuard
-> 内存版 repositories
-> 返回 typed result
```

注意：这还不是完整的心理咨询 agent。

它现在更像是一个“接口和主流程骨架”，作用是让后续同事可以分别替换真实数据库、真实 LLM、真实 loop 能力和真实本地部署接入层。

当前已经通过：

```bash
ruff check .
mypy .
pytest
```

当前测试文件：

```text
tests/integration/test_turn_flow.py
tests/unit/test_state_reducer.py
```

## 1. 重要调整：我们不以 HTTP API 作为主接入方式

你们当前目标是接到本地部署的系统上，所以主入口应该是 Python 层的本地调用，而不是 HTTP API。

主入口是：

```python
await TurnOrchestrator.handle_turn(
    user_id=UserId(...),
    session_id=SessionId(...),
    text="...",
)
```

或者后续封装一个本地 adapter，例如：

```text
local_runtime/
local_adapter.py
```

HTTP API 现在可以保留为：

```text
本地调试入口
demo 入口
以后需要服务化时的备选入口
```

但后续任务不要默认围绕 API 展开。优先围绕本地部署集成方式展开。

## 2. 协作规则

### 规则 1：公共 contract 受保护

这些文件定义了大家之间的公共接口：

```text
schemas/
orchestrator/turn_orchestrator.py
docs/contracts/README.md
```

普通业务开发不要随手改这些文件。

如果确实需要改公共 contract，请单独提一个“Contract PR”或单独 commit，说明：

```text
为什么必须改 contract
会影响哪些同事
更新了哪些测试
其他人需要如何迁移
```

### 规则 2：每个人尽量只改自己负责的文件

每个同事都应该主要在自己的目录里工作。这样可以减少 merge conflict。

### 规则 3：Fake 实现先不要删

不要删除 fake agents 或 in-memory repositories。

它们现在用于：

```text
本地测试
集成测试
没有真实 LLM 时跑通主链路
没有真实数据库时跑 demo
```

等真实实现稳定后，再考虑是否下线 fake 实现。

### 规则 4：所有公共函数都要写清楚类型

新增的公共函数或方法应该写清楚输入和输出类型，例如：

```python
async def example(payload: SomeInput) -> SomeOutput:
    ...
```

尽量不要使用没有约束的 `dict`、`Any` 或含糊的返回值。

### 规则 5：每个任务都要加测试或更新测试

每个同事完成任务后，至少要跑：

```bash
ruff check .
mypy .
pytest
```

## 3. 推荐分支

建议每个人开独立分支：

```text
person-a-contracts-integration
person-b-storage-local-runtime
person-c-llm-agents
person-d-adaptive-loop
```

如果用 Codex 默认分支前缀，可以用：

```text
codex/contracts-integration
codex/storage-local-runtime
codex/llm-agents
codex/adaptive-loop
```

## 4. Person A：Contract / 主流程 / 最终整合负责人

### 角色定位

Person A 负责公共接口、主流程边界和最后整合。

Person A 不需要亲自实现所有 loop 细节，但要保证 loop 能正确接入主流程。

Person A 主要保护这些文件：

```text
schemas/
orchestrator/turn_orchestrator.py
services/state_reducer.py
services/context_builder.py
docs/contracts/
tests/integration/
```

### Task A1：维护公共 contracts

负责文件：

```text
schemas/
docs/contracts/README.md
```

具体任务：

1. 审核其他同事提出的 schema 修改请求。
2. 确保每个 schema 都是 Pydantic model 或 StrEnum。
3. 确保字段名清楚，含义稳定。
4. 重要的派生信息要能追溯到 source message id。
5. 公共接口变化时，同步更新 `docs/contracts/README.md`。

验收标准：

```text
ruff check . 通过
mypy . 通过
pytest 通过
其他同事不需要猜 payload 结构
```

### Task A2：维护 TurnOrchestrator 主流程

负责文件：

```text
orchestrator/turn_orchestrator.py
tests/integration/test_turn_flow.py
```

具体任务：

1. 保持单轮主链路稳定。
2. 接收 B/C/D 的实现，但不让它们绕过 orchestrator。
3. 保证所有状态变更都经过 `StateReducer`。
4. 保证所有回复都经过 `OutputGuard`。
5. 保证非 normal risk route 进入安全路径。

验收标准：

```text
fake pipeline 继续通过
真实模块逐步接入时，主流程不需要大改
```

### Task A3：维护本地部署接入层

建议新增文件：

```text
local_runtime/__init__.py
local_runtime/adapter.py
local_runtime/config.py
```

具体任务：

1. 提供一个本地调用入口，封装 `TurnOrchestrator.handle_turn()`。
2. 输入输出仍然使用现有 schemas。
3. 不依赖 HTTP。
4. 可以被你们本地部署系统直接 import。

建议接口：

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

验收标准：

```text
不启动 FastAPI 也能本地调用 agent
local adapter 复用同一套 orchestrator
integration test 覆盖 local adapter
```

## 5. Person B：Storage / Repository / 本地持久化负责人

### 角色定位

Person B 负责把内存版持久化替换成真实本地持久化方案。

如果你们本地部署用 PostgreSQL，就做 PostgreSQL。

如果你们本地部署更适合 SQLite，也可以先做 SQLite，但要先和 Person A 确认。

Person B 主要负责：

```text
storage/
app/config.py
alembic.ini
```

如果后续仍保留 FastAPI demo，Person B 可以少量维护：

```text
api/
```

但 API 不是主任务。

### 不要修改

```text
schemas/
orchestrator/turn_orchestrator.py
agents/
services/state_reducer.py
```

如果发现 repository protocol 不够用，先找 Person A 讨论 contract change。

### Task B1：本地持久化基础设施

负责文件：

```text
storage/database.py
app/config.py
alembic.ini
```

具体任务：

1. 添加 SQLAlchemy async engine。
2. 添加 async session factory。
3. 在 config 中添加 `DATABASE_URL`。
4. 支持本地部署环境读取配置。
5. 数据库连接逻辑不要和业务逻辑混在一起。

验收标准：

```text
本地环境可以创建 async database session
Database URL 来自 config/env
agent 文件不会直接 import SQLAlchemy
```

### Task B2：第一阶段 SQLAlchemy models

负责文件：

```text
storage/models/user.py
storage/models/session.py
storage/models/message.py
storage/models/session_state.py
storage/models/intervention.py
storage/models/__init__.py
```

需要建的表：

```text
users
sessions
messages
session_state_versions
intervention_events
```

具体任务：

1. 第一版主键可以先用 string。
2. raw messages 必须 append-only。
3. session state 用 versioned JSON 存储。
4. intervention events 要支持 pending/evaluated 状态。
5. 所有核心表加 timestamp。

验收标准：

```text
Alembic 可以生成或运行 migration
Models 可以正常 import
没有循环 import
```

### Task B3：数据库版 repositories

负责文件：

```text
storage/repositories/message_repository.py
storage/repositories/state_repository.py
storage/repositories/intervention_repository.py
storage/repositories/summary_repository.py
```

具体任务：

实现数据库版 repository，但保留现有 in-memory repository。

需要满足这些现有方法：

```python
create_user_message(...)
create_assistant_message(...)
get_recent(...)
get_by_id(...)

get_current(...)
save_version(...)

get_pending(...)
create_pending(...)
complete(...)
```

重要约束：

1. 保留 in-memory repositories，测试还要用。
2. 不要未经 Person A 同意改公共 method name。
3. repository 返回 Pydantic schema 对象，不要把 SQLAlchemy model 直接返回给 orchestrator。

验收标准：

```text
Repository tests 可以通过 test database
现有 fake pipeline 仍然能用 in-memory repositories 跑通
数据库版 repositories 满足同一套 protocol
```

### Task B4：本地部署配置和初始化脚本

建议负责文件：

```text
scripts/init_local_db.py
scripts/reset_local_state.py
.env.example
README.md
```

具体任务：

1. 写本地数据库初始化脚本。
2. 写重置本地测试数据脚本。
3. 更新 `.env.example`。
4. README 中说明本地持久化如何配置。

验收标准：

```text
新同事可以在本地初始化数据库
不需要启动 HTTP API 也可以跑本地 agent
```

## 6. Person C：LLM Client / Real Online Agents / Prompts 负责人

### 角色定位

Person C 负责把主要 fake agent 替换或扩展为真实模型实现，但保持公共接口不变。

Person C 主要负责：

```text
llm/
agents/risk_agent.py
agents/state_tracker.py
agents/strategy_planner.py
agents/response_agent.py
agents/output_guard.py
prompts/risk_agent.md
prompts/state_tracker.md
prompts/strategy_planner.md
prompts/response_agent.md
prompts/output_guard.md
tests/contract/
```

### 不要修改

```text
storage/
orchestrator/turn_orchestrator.py
services/state_reducer.py
```

agents 不允许直接写数据库，也不允许直接修改 `SessionState`。

### Task C1：LLM client

负责文件：

```text
llm/client.py
llm/model_registry.py
llm/retry_policy.py
llm/structured_output.py
llm/exceptions.py
```

具体任务：

1. 实现 OpenAI-compatible async client wrapper。
2. 支持按 agent name 查 model name。
3. 支持 timeout 和 retry。
4. 支持 Pydantic structured output validation。
5. 如果模型输出非法 JSON，要 retry 或返回安全 fallback。

建议公共方法：

```python
async def generate_text(...) -> str:
    ...

async def generate_structured(
    ...,
    output_type: type[OutputT],
) -> OutputT:
    ...
```

验收标准：

```text
LLM client 可以用 mock transport 测试
structured output 返回 Pydantic objects
模型失败不会导致整个 orchestrator 崩溃
```

### Task C2：真实 RiskAgent

负责文件：

```text
agents/risk_agent.py
prompts/risk_agent.md
tests/contract/test_risk_agent.py
```

具体任务：

1. 保留 `FakeRiskAgent`。
2. 新增 model-backed `RiskAgent` class。
3. 输入必须是 `RiskInput`。
4. 输出必须是 `RiskResult`。
5. 如果模型输出非法，返回保守 fallback：

```text
risk_level = medium 或 high
route = safety_clarification
needs_clarification = true
```

验收标准：

```text
Contract test 能验证 RiskResult
危机场景不能 route 到 normal_dialogue
普通消息可以 route 到 normal_dialogue
```

### Task C3：真实 StateTracker

负责文件：

```text
agents/state_tracker.py
prompts/state_tracker.md
tests/contract/test_state_tracker.py
```

具体任务：

1. 保留 `FakeStateTracker`。
2. 新增 model-backed `StateTracker`。
3. 输入必须是 `StateTrackerInput`。
4. 输出必须是 `StateDelta`。
5. StateTracker 只能输出 delta，不能返回完整 SessionState。
6. 用户明确陈述和模型假设必须分开。

验收标准：

```text
Contract test 能验证 StateDelta
用户纠正会进入 user_corrections
emotion reports 带 source_message_id
StateTracker 不访问数据库
```

### Task C4：真实 StrategyPlanner

负责文件：

```text
agents/strategy_planner.py
prompts/strategy_planner.md
tests/contract/test_strategy_planner.py
```

具体任务：

1. 保留 `FakeStrategyPlanner`。
2. 新增 model-backed `StrategyPlanner`。
3. 输入必须是 `StrategyPlannerInput`。
4. 输出必须是 `StrategyPlan`。
5. 使用 feedback 避免重复已经被用户拒绝的策略。

验收标准：

```text
负反馈可以触发策略切换
StrategyPlan 使用允许的 StrategyType
avoid list 在风险或 poor-fit 场景下有内容
```

### Task C5：真实 ResponseAgent 和 OutputGuard

负责文件：

```text
agents/response_agent.py
agents/output_guard.py
prompts/response_agent.md
prompts/output_guard.md
tests/contract/test_response_and_guard.py
```

具体任务：

1. ResponseAgent 消费 `ResponseContext`。
2. ResponseAgent 返回 `DraftResponse`。
3. OutputGuard 消费 `GuardInput`。
4. OutputGuard 返回 `GuardResult`。
5. OutputGuard 要能阻止诊断、药物建议、制造依赖、安全流程绕过等输出。

验收标准：

```text
ResponseAgent 不修改 state
OutputGuard 可以 block 或 rewrite 不安全 draft
Contract tests 能验证所有返回 schema
```

## 7. Person D：Adaptive Loop / Summary / Memory Loop 负责人

### 角色定位

Person D 负责 loop 相关能力。

这里的 loop 指：

```text
上一轮 intervention
-> 当前用户反馈
-> FeedbackEvaluator
-> StateReducer 消费 feedback
-> StrategyPlanner 切换策略
-> RollingSummary 压缩长对话
-> SessionFinalizer 结束会话
-> MemoryCurator 提取候选长期记忆
-> MemoryPolicy 审核写入
-> 下一次 session bootstrap
```

Person D 主要负责：

```text
agents/feedback_evaluator.py
agents/rolling_summarizer.py
agents/session_finalizer.py
agents/memory_curator.py
prompts/feedback_evaluator.md
prompts/rolling_summarizer.md
prompts/session_finalizer.md
prompts/memory_curator.md
services/memory_policy.py
services/conflict_resolver.py
workers/summary_worker.py
workers/memory_worker.py
workers/session_close_worker.py
tests/end_to_end/
evaluation/
```

### 不要修改

```text
schemas/
orchestrator/turn_orchestrator.py
```

如果 loop 需要新的字段，先找 Person A 做 contract change。

### Task D1：真实 FeedbackEvaluator

负责文件：

```text
agents/feedback_evaluator.py
prompts/feedback_evaluator.md
tests/contract/test_feedback_evaluator.py
```

具体任务：

1. 保留 `FakeFeedbackEvaluator`。
2. 新增 model-backed `FeedbackEvaluator`。
3. 输入必须是 `FeedbackInput`。
4. 输出必须是 `FeedbackResult`。
5. 只判断可观察反馈，不判断“治疗成功”。

验收标准：

```text
用户明确拒绝上一轮方向时，strategy_fit = poor
用户只是继续聊天时，不自动算作 positive feedback
Contract test 能验证 FeedbackResult
```

### Task D2：完善策略反馈闭环测试

负责文件：

```text
tests/end_to_end/test_adaptive_loop.py
```

具体任务：

增加多轮测试：

1. 第一轮 assistant 使用某个 strategy。
2. 创建 pending intervention。
3. 第二轮用户拒绝上一轮方式。
4. FeedbackEvaluator 标记 poor fit。
5. StrategyPlanner 换策略。
6. 新 intervention 记录新的 strategy。

验收标准：

```text
测试证明 pending intervention 会被消费
测试证明 negative feedback 会影响下一轮 strategy
测试不依赖真实数据库
```

### Task D3：RollingSummarizer

负责文件：

```text
agents/rolling_summarizer.py
prompts/rolling_summarizer.md
workers/summary_worker.py
tests/contract/test_rolling_summarizer.py
evaluation/evaluators/summary_evaluator.py
```

具体任务：

1. 定义或使用现有 `RollingSummary` schema。
2. 读取旧 summary 和未覆盖 messages。
3. 生成新 summary。
4. summary 中的重要结论必须有 source message ids。
5. 不允许凭空添加用户没说过的事实。

验收标准：

```text
长对话可以生成 rolling summary
summary 覆盖范围可追踪
summary 不覆盖当前最新原话
有 summary faithfulness 测试或评测骨架
```

### Task D4：SessionFinalizer

负责文件：

```text
agents/session_finalizer.py
prompts/session_finalizer.md
workers/session_close_worker.py
tests/contract/test_session_finalizer.py
```

具体任务：

1. 会话结束时生成结构化总结。
2. 提取 unfinished topics。
3. 提取 action items。
4. 提取 candidate memories。
5. 提取 strategy outcomes。

验收标准：

```text
Session close 后可以生成结构化结果
不会写入长期记忆，只产生候选项
候选项有 source message ids
```

### Task D5：MemoryCurator + MemoryPolicy

负责文件：

```text
agents/memory_curator.py
prompts/memory_curator.md
services/memory_policy.py
services/conflict_resolver.py
workers/memory_worker.py
tests/unit/test_memory_policy.py
tests/contract/test_memory_curator.py
```

具体任务：

1. MemoryCurator 只产生 `MemoryCandidate`。
2. MemoryPolicy 决定是否允许 create/reinforce/supersede/expire/delete。
3. 敏感信息默认需要更严格审核。
4. 单次互动不能直接形成稳定 procedural memory。
5. 用户明确删除或纠正的信息要优先。

验收标准：

```text
候选记忆不会直接写入 memory store
Policy 可以拒绝敏感或低置信候选
ConflictResolver 可以标记新旧记忆冲突
Memory tests 覆盖 create/reject/conflict/supersede
```

### Task D6：Loop evaluation

负责文件：

```text
evaluation/run_eval.py
evaluation/evaluators/state_evaluator.py
evaluation/evaluators/strategy_evaluator.py
evaluation/evaluators/memory_evaluator.py
evaluation/evaluators/summary_evaluator.py
```

具体任务：

1. 建立 loop 相关 evaluation cases。
2. 覆盖 strategy switching。
3. 覆盖 summary faithfulness。
4. 覆盖 memory consistency。
5. 输出简单 report。

验收标准：

```text
python -m evaluation.run_eval 可以运行
没有真实 LLM 时也能跑 fake cases
报告包含 total/pass/fail
```

## 8. 推荐整合顺序

### Step 1：冻结当前 base

当前绿色命令：

```bash
ruff check .
mypy .
pytest
```

建议先把当前版本 commit 或 tag 成 baseline。

### Step 2：先合低风险基础设施

推荐顺序：

```text
A3 local runtime adapter
B1 storage foundation
C1 LLM client with mock tests
D1 FeedbackEvaluator contract tests
```

### Step 3：再合可替换实现

推荐顺序：

```text
B2 database models
B3 database repositories
C2 RiskAgent
C3 StateTracker
C4 StrategyPlanner
C5 ResponseAgent + OutputGuard
D2 adaptive loop E2E tests
```

### Step 4：再加长对话和跨 session loop

推荐顺序：

```text
D3 RollingSummarizer
D4 SessionFinalizer
D5 MemoryCurator + MemoryPolicy
D6 Loop evaluation
A2 StateReducer improvements
A2/A3 integration updates
```

## 9. 每个任务的 Definition of Done

每个任务完成时，需要报告：

```text
目标
改了哪些文件
是否影响公共接口
是否影响数据库 migration
新增或更新了哪些测试
运行了哪些命令
已知限制
后续任务
```

每个任务都应该运行：

```bash
ruff check .
mypy .
pytest
```

如果有命令失败，需要贴出失败输出并说明原因。

## 10. 给同事使用 Codex 的示例 prompt

### Person B 示例 prompt

```text
请先阅读 README.md 和 docs/contracts/README.md。

任务：实现 database-backed MessageRepository，同时保留 in-memory repository。

允许修改：
- storage/database.py
- storage/models/message.py
- storage/repositories/message_repository.py
- tests/unit/test_message_repository.py

不要修改：
- schemas/
- orchestrator/turn_orchestrator.py
- agents/

要求：
- 保持现有 repository protocol 方法名不变
- 返回 schemas.messages.Message 对象
- 保持 append-only message 行为
- 添加测试
- 运行 ruff check ., mypy ., pytest
```

### Person C 示例 prompt

```text
请先阅读 README.md 和 docs/contracts/README.md。

任务：实现 model-backed RiskAgent，同时保留 FakeRiskAgent。

允许修改：
- agents/risk_agent.py
- prompts/risk_agent.md
- llm/client.py
- llm/structured_output.py
- tests/contract/test_risk_agent.py

不要修改：
- schemas/
- orchestrator/turn_orchestrator.py
- storage/

要求：
- RiskAgent 输入是 RiskInput
- RiskAgent 输出是 RiskResult
- 模型输出非法时 fallback 到 safety_clarification
- 添加 contract tests
- 运行 ruff check ., mypy ., pytest
```

### Person D 示例 prompt

```text
请先阅读 README.md 和 docs/contracts/README.md。

任务：完善 FeedbackEvaluator 和 adaptive loop 测试。

允许修改：
- agents/feedback_evaluator.py
- prompts/feedback_evaluator.md
- tests/contract/test_feedback_evaluator.py
- tests/end_to_end/test_adaptive_loop.py

不要修改：
- schemas/
- orchestrator/turn_orchestrator.py
- storage/

要求：
- 保留 FakeFeedbackEvaluator
- 新增 model-backed FeedbackEvaluator
- 输入是 FeedbackInput
- 输出是 FeedbackResult
- 用户明确拒绝上一轮策略时 strategy_fit = poor
- 不把用户继续聊天自动当作 positive feedback
- 添加 contract 和 E2E tests
- 运行 ruff check ., mypy ., pytest
```

## 11. 不要做什么

不要这样做：

```text
一个人改 schemas，另一个人同时写 agents
agents 直接写数据库
ResponseAgent 修改 SessionState
数据库 repository 把 SQLAlchemy model 直接返回给 orchestrator
业务 PR 顺手改 TurnOrchestrator 主流程
真实实现没稳定前删除 fake 实现
把 HTTP API 当成唯一入口
```

应该这样做：

```text
保持 contracts 稳定
真实实现先和 fake 实现并存
用 repositories 作为持久化边界
用 StateReducer 作为状态修改边界
发送回复前必须经过 OutputGuard
本地部署优先走 local adapter 或 orchestrator Python 调用
保持小 PR
整合前先跑测试
```
