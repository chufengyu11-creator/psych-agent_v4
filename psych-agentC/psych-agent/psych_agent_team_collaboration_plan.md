# 心理支持对话 Agent：四人协作与 Codex 分工方案

> 本文用于指导四名开发者与各自的 Codex 协作开发。  
> 核心原则：**先冻结薄公共底座，再分布式并行；公共 Contract 受控，内部实现独立。**

---

## 1. 总体协作策略

本项目可以分布式开发，但不能从“所有文件为空”的状态直接四路自由并行。

推荐流程：

```text
薄公共底座
→ Contract 冻结
→ 四路并行
→ 高频小集成
→ Adaptive Loop
→ 跨会话能力
→ Demo 与评测
```

不推荐：

```text
四个人从空文件开始各自定义 Schema 和接口
```

也不推荐：

```text
一个人先把整个基础系统写完，其他人再开始
```

最合适的方式是：

- 架构负责人先完成最薄的一层公共 Contract；
- 其他成员同时完成不依赖业务 Contract 的基础模块；
- 公共接口冻结后，立即进入四路并行；
- 架构负责人随后主做 Adaptive Loop；
- 架构负责人继续保留公共 Contract 和 Orchestrator 的最终所有权；
- 每天至少进行一次小集成；
- 所有跨模块变更通过小 PR 完成。

---

# 2. 核心角色调整

本项目中，人员 A 不只是做架构底座。

人员 A 的完整定位是：

> **Architecture Owner + Adaptive Dialogue Loop Owner**

时间分配建议：

```text
约 20%：
- 公共 Contract
- Orchestrator
- 跨模块集成
- 架构决策

约 80%：
- State
- Context
- Feedback Loop
- Rolling Summary
- Cross-session Memory
```

也就是说：

```text
先完成 A 的薄底座
→ 再主做原计划中的 D
→ 同时继续维护 A 的公共接口职责
```

原本独立的 D 角色拆分为两部分：

```text
Adaptive Loop 核心部分
→ 由人员 A 负责

Platform / Evaluation / Integration Support
→ 由人员 D 负责
```

---

# 3. 第一阶段公共底座

公共底座由人员 A 主导，建议在半天到一天内完成。

## 3.1 必须先确定的公共 Schema

第一版只冻结以下结构：

```text
Message
RiskResult
StateDelta
SessionState
StrategyPlan
ResponseContext
DraftResponse
GuardResult
InterventionRecord
```

对应文件：

```text
schemas/messages.py
schemas/risk.py
schemas/state.py
schemas/strategy.py
schemas/context.py
schemas/safety.py
schemas/intervention.py
```

第一版目标不是字段完美，而是所有模块使用同一套输入输出。

---

## 3.2 必须先定义的 Agent 接口

对应文件：

```text
agents/base.py
```

建议统一为异步接口：

```python
from typing import Generic, Protocol, TypeVar

InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")


class Agent(Protocol, Generic[InputT, OutputT]):
    async def run(self, payload: InputT) -> OutputT:
        ...
```

外部调用接口示例：

```python
class RiskAgent:
    async def analyze(self, payload: RiskInput) -> RiskResult:
        ...
```

```python
class StateTracker:
    async def extract_delta(
        self,
        payload: StateTrackerInput,
    ) -> StateDelta:
        ...
```

```python
class StrategyPlanner:
    async def plan(
        self,
        payload: StrategyPlannerInput,
    ) -> StrategyPlan:
        ...
```

---

## 3.3 必须先定义的 Repository 接口

人员 A 只定义 Orchestrator 需要什么，不实现数据库。

建议至少定义：

```text
MessageRepository
StateRepository
InterventionRepository
SummaryRepository
```

核心接口：

```python
class MessageRepository(Protocol):
    async def create_user_message(...): ...
    async def create_assistant_message(...): ...
    async def get_recent(...): ...
```

```python
class StateRepository(Protocol):
    async def get_current(...): ...
    async def save_version(...): ...
```

```python
class InterventionRepository(Protocol):
    async def get_pending(...): ...
    async def create_pending(...): ...
    async def complete(...): ...
```

---

## 3.4 Fake Orchestrator 集成链路

在真实 Agent 和数据库完成前，必须先用 Fake 实现跑通：

```text
用户消息
→ Fake Risk Agent
→ Fake State Tracker
→ State Reducer
→ Fake Strategy Planner
→ Context Builder
→ Fake Response Agent
→ Fake Output Guard
→ 返回回复
```

对应测试：

```text
tests/integration/test_turn_flow.py
```

这条测试通过后，四个人才进入正式并行阶段。

---

# 4. 四人职责划分

## 人员 A：Architecture + Adaptive Dialogue Loop

### 角色定位

负责系统总装、公共接口，以及项目最核心的自适应对话循环。

### 主要负责目录

```text
schemas/
orchestrator/
docs/contracts/
docs/decisions/
app/dependencies.py

services/state_reducer.py
services/context_builder.py
services/token_budget.py
services/memory_policy.py
services/conflict_resolver.py

agents/feedback_evaluator.py
agents/rolling_summarizer.py
agents/session_finalizer.py
agents/memory_curator.py

tests/integration/
tests/end_to_end/
```

### 第一部分职责：公共底座与架构

- 定义和冻结公共 Pydantic Schema；
- 定义 Agent 与 Repository 接口；
- 实现 `TurnOrchestrator`；
- 决定各模块生产—消费顺序；
- 维护依赖注入；
- 接入其他成员的模块；
- 处理跨模块接口冲突；
- 审核所有公共 Contract 变更；
- 维护架构决策文档；
- 维护集成测试和 E2E。

### 第二部分职责：Adaptive Dialogue Loop

负责以下完整链路：

```text
Previous Intervention
→ Feedback Evaluator
→ Feedback Result
→ State Tracker produces StateDelta
→ State Reducer
→ New Session State
→ Context Builder
→ Strategy Planner
→ New Intervention
```

具体包括：

- 用户反馈如何被识别；
- Session State 如何变化；
- 用户纠正如何覆盖旧状态；
- 策略何时继续、切换或暂停；
- 当前哪些内容进入 Context；
- 何时触发 Rolling Summary；
- Session 结束时提取什么；
- 哪些候选信息可以进入长期记忆；
- 新旧长期状态如何冲突、替代和过期；
- 新 Session 如何恢复目标、偏好和未完成话题。

### 第一阶段交付物

```text
schemas/messages.py
schemas/risk.py
schemas/state.py
schemas/strategy.py
schemas/context.py
schemas/safety.py
schemas/intervention.py

agents/base.py
orchestrator/turn_orchestrator.py

services/state_reducer.py
services/context_builder.py
services/token_budget.py

docs/contracts/
tests/integration/test_turn_flow.py
```

### 第二阶段交付物

```text
agents/feedback_evaluator.py
agents/rolling_summarizer.py

services/conflict_resolver.py

tests/unit/test_state_reducer.py
tests/unit/test_context_builder.py
evaluation/datasets/strategy_switching/
evaluation/datasets/summary_faithfulness/
```

### 第三阶段交付物

```text
agents/session_finalizer.py
agents/memory_curator.py

services/memory_policy.py
services/conflict_resolver.py

跨 Session Bootstrap
Memory Consistency Logic
```

### 关键约束

- 不需要亲自实现所有数据库细节；
- 不负责所有 Prompt；
- State Reducer 不调用 LLM；
- Context Builder 不修改数据库；
- Memory Curator 只提出候选；
- Memory 写入必须经过 Memory Policy；
- 一次用户反馈不能直接形成稳定 Procedural Memory。

---

## 人员 B：Database、Repository、Session 和 API

### 角色定位

负责持久化、API 和后端基础设施。

### 主要负责目录

```text
storage/
api/
app/main.py
app/config.py
app/lifecycle.py
alembic.ini
```

### 核心职责

- PostgreSQL 异步连接；
- SQLAlchemy Base；
- Alembic migrations；
- 用户、Session、Message 数据表；
- Session State 版本表；
- Intervention 表；
- Summary 和 Memory 数据表；
- Repository 实现；
- Chat、Session、Memory API；
- 消息 append-only；
- 数据库单元测试。

### 第一阶段只实现的数据表

```text
users
sessions
messages
session_state_versions
intervention_events
```

### 第一阶段交付物

```text
storage/database.py

storage/models/user.py
storage/models/session.py
storage/models/message.py
storage/models/session_state.py
storage/models/intervention.py

storage/repositories/user_repository.py
storage/repositories/session_repository.py
storage/repositories/message_repository.py
storage/repositories/state_repository.py
storage/repositories/intervention_repository.py

api/routers/chat.py
api/routers/sessions.py
api/routers/health.py
```

### 第二阶段交付物

```text
storage/models/summary.py
storage/repositories/summary_repository.py

api/routers/sessions.py
Session Close API
Summary API
```

### 第三阶段交付物

```text
storage/models/memory.py
storage/repositories/memory_repository.py
api/routers/memories.py
```

### 对外提供的核心接口

```python
await message_repository.create_user_message(...)
await message_repository.create_assistant_message(...)
await message_repository.get_recent(...)

await state_repository.get_current(...)
await state_repository.save_version(...)

await intervention_repository.get_pending(...)
await intervention_repository.create_pending(...)
await intervention_repository.complete(...)

await summary_repository.get_current(...)
await summary_repository.save_version(...)

await memory_repository.search_active(...)
await memory_repository.create(...)
await memory_repository.delete_for_user(...)
```

### 不负责

- 对话策略；
- Prompt；
- 状态语义提取；
- Memory 写入决策。

---

## 人员 C：LLM Client、Online Agents 和 Safety

### 角色定位

负责模型理解、生成、结构化输出和安全审查。

### 主要负责目录

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
```

### 核心职责

- 统一 LLM Client；
- 对接 vLLM/OpenAI-compatible API；
- Structured Output；
- Pydantic 验证；
- 非法 JSON 修复；
- 超时和重试；
- Risk Agent；
- State Tracker；
- Strategy Planner；
- Response Agent；
- Output Guard；
- Prompt 版本；
- Agent contract tests。

### 第一阶段交付物

```text
llm/client.py
llm/structured_output.py
llm/retry_policy.py
llm/exceptions.py

agents/risk_agent.py
agents/state_tracker.py
agents/strategy_planner.py
agents/response_agent.py
agents/output_guard.py
```

### 每个 Agent 必须同时提供

```text
真实实现或可替换实现
Fake 实现
输入 Schema
输出 Schema
Contract Test
```

### 与人员 A 的接口关系

人员 C 负责：

```text
模型如何实现 State Tracker
模型如何实现 Strategy Planner
模型如何生成 Response
```

人员 A 负责：

```text
State Tracker 吃什么输入
StateDelta 如何被消费
Feedback 如何影响 Planner
StrategyPlan 如何进入下一轮 Context
```

### 约束

- 不直接访问数据库；
- 不修改状态；
- 不写入长期记忆；
- 不绕过 Orchestrator；
- 不自行修改公共 Schema。

---

## 人员 D：Platform、Evaluation 和 Integration Support

### 角色定位

负责工程运行环境、测试评测、集成支持和 Demo 可用性。

### 主要负责目录

```text
app/
workers/
evaluation/
tests/
.github/
scripts/

services/audit_logger.py

Docker / Compose / Deployment
Logging / Monitoring
```

### 核心职责

- FastAPI 应用启动与生命周期；
- 后台 Worker 基础；
- CI；
- Docker / Docker Compose；
- 测试基础设施；
- Contract Tests；
- Integration / E2E Support；
- Evaluation Runner；
- Synthetic Evaluation Cases；
- Audit Logger；
- Agent 调用日志；
- Demo 环境；
- 部署脚本；
- 协助处理 PR 冲突与联调。

### 第一阶段交付物

```text
app/main.py
app/config.py
app/lifecycle.py

.github/workflows/ci.yml

tests/contract/
tests/integration/

evaluation/run_eval.py
evaluation/datasets/

services/audit_logger.py
```

### 第二阶段交付物

```text
workers/worker_app.py
workers/post_turn_worker.py
workers/summary_worker.py

evaluation/evaluators/state_evaluator.py
evaluation/evaluators/strategy_evaluator.py
evaluation/evaluators/summary_evaluator.py
```

### 第三阶段交付物

```text
workers/memory_worker.py
workers/session_close_worker.py

evaluation/evaluators/memory_evaluator.py
evaluation/evaluators/safety_evaluator.py

Dockerfile
docker-compose.yml
Demo startup scripts
```

### Integration Support

人员 D 还负责：

- 检查 B、C、A 的模块能否在同一环境运行；
- 建立统一 Fake Fixtures；
- 建立模型 mock；
- 维护测试数据库；
- 运行每天的集成测试；
- 记录接口不一致；
- 将问题反馈给人员 A；
- 协助修复 CI 和环境问题。

### 不负责

- 定义公共业务 Schema；
- 决定 State 或 Memory 语义；
- 编写主要心理对话 Prompt；
- 自行修改 Orchestrator 主流程。

---

# 5. 生产—消费链路

```text
人员 B
生产：
- Message
- Previous SessionState
- Pending Intervention
- Summary / Memory persistence
        ↓
人员 C
生产：
- RiskResult
- StateDelta
- StrategyPlan
- DraftResponse
- GuardResult
        ↓
人员 A
生产：
- New SessionState
- ResponseContext
- FeedbackResult
- RollingSummary
- MemoryCandidate
- Complete Adaptive Loop
        ↓
人员 D
负责：
- Test Harness
- Evaluation
- Audit
- CI
- Workers
- Demo Runtime
```

模块关系表：

| 生产者 | 生产内容 | 消费者 |
|---|---|---|
| B | Message、旧 SessionState、Pending Intervention | A、C |
| C | RiskResult、StateDelta、StrategyPlan、DraftResponse | A |
| A | 新 SessionState、Context、Feedback、Summary、MemoryCandidate | B、C、D |
| D | 测试结果、评测报告、运行环境、审计日志 | A、B、C |
| A | 完整运行链路 | API、测试、Demo |
| B | 持久化 | 全部模块 |
| C | 模型行为 | Orchestrator |
| D | 运行与质量保障 | 全部模块 |

---

# 6. 公共文件修改规则

以下文件属于公共 Contract：

```text
schemas/common.py
schemas/messages.py
schemas/risk.py
schemas/state.py
schemas/strategy.py
schemas/context.py
schemas/intervention.py
schemas/safety.py
agents/base.py
orchestrator/turn_orchestrator.py
docs/contracts/
```

任何人需要修改时：

1. 先创建一个专门的 Contract Issue；
2. 说明修改原因；
3. 单独提交小 PR；
4. 人员 A 批准；
5. 合并后其他模块同步更新；
6. 不允许在业务 PR 中顺手修改公共 Contract。

人员 A 主做 Adaptive Loop 后，仍然保留这些文件的最终决定权。

---

# 7. 文件所有权

## 人员 A 拥有

```text
schemas/
orchestrator/
docs/contracts/
docs/decisions/

services/state_reducer.py
services/context_builder.py
services/token_budget.py
services/memory_policy.py
services/conflict_resolver.py

agents/feedback_evaluator.py
agents/rolling_summarizer.py
agents/session_finalizer.py
agents/memory_curator.py

tests/end_to_end/
```

## 人员 B 拥有

```text
storage/
api/

app/config.py
alembic.ini
```

## 人员 C 拥有

```text
llm/

agents/risk_agent.py
agents/state_tracker.py
agents/strategy_planner.py
agents/response_agent.py
agents/output_guard.py

prompts/
```

## 人员 D 拥有

```text
app/main.py
app/lifecycle.py

workers/
evaluation/
.github/
scripts/

tests/contract/
tests/integration/

services/audit_logger.py

Docker / Deployment files
```

跨 ownership 修改必须在 PR 描述中明确说明。

---

# 8. 分布式开发时间线

## Day 0：公共底座

### 人员 A

- 公共 Schema；
- Agent/Repository 接口；
- Fake Orchestrator；
- Integration Test；
- State Reducer 和 Context Builder 的接口。

### 人员 B

- SQLAlchemy Base；
- Database Session；
- Alembic 初始化；
- users/sessions/messages 基础模型。

### 人员 C

- LLM Client；
- Structured Output；
- Retry Policy；
- Fake Agent 基类。

### 人员 D

- CI；
- 测试环境；
- Contract Test 骨架；
- Evaluation Dataset 骨架；
- Fake Fixtures。

目标：

```text
所有模块接口可插拔
```

---

## Day 1–2：第一轮并行

### 人员 A

- 完整 Fake Orchestrator；
- State Reducer；
- Context Builder；
- Token Budget；
- 依赖注入；
- Integration Test。

### 人员 B

- Database Models；
- Repositories；
- Chat API；
- Session API。

### 人员 C

- Fake Risk Agent；
- Fake State Tracker；
- Fake Strategy Planner；
- Fake Response Agent；
- Fake Output Guard。

### 人员 D

- CI 完善；
- Contract Tests；
- Test Database；
- Audit Logger；
- Fake Pipeline Test Support。

第一次集成目标：

```text
POST /chat/turn
→ 保存用户消息
→ Fake Risk Agent
→ Fake State Tracker
→ State Reducer
→ Fake Strategy Planner
→ Context Builder
→ Fake Response Agent
→ Fake Output Guard
→ 保存回复
→ 返回 JSON
```

---

## Day 3–4：Adaptive Loop

### 人员 A

- Feedback Evaluator；
- Intervention Ledger 逻辑；
- Strategy Switching；
- Rolling Summary；
- Loop 集成；
- Loop E2E。

### 人员 B

- Summary Repository；
- Intervention Repository；
- Session Close API。

### 人员 C

- 接入真实模型；
- State Tracker Prompt；
- Strategy Prompt；
- Response Prompt；
- Output Guard。

### 人员 D

- Workers；
- Strategy Switching Evaluation；
- Summary Faithfulness Evaluation；
- 集成环境；
- 回归测试。

目标：

```text
系统可根据下一轮用户反馈切换策略
```

---

## Day 5–7：跨会话与 Demo

### 人员 A

- Session Finalizer；
- Memory Curator；
- Memory Policy；
- Conflict Resolver；
- Cross-session Bootstrap；
- 跨 Session 总集成。

### 人员 B

- Memory Model；
- Memory Repository；
- Memory API；
- 用户查看和删除记忆。

### 人员 C

- Session Finalizer 模型调用；
- Memory Curator 模型调用；
- Prompt 版本化；
- 高风险路径完善。

### 人员 D

- Memory Consistency Evaluation；
- Safety Evaluation；
- Docker / Compose；
- Demo Flow；
- README 启动说明；
- 完整回归测试。

目标：

```text
关闭 Session
→ 生成摘要和候选记忆
→ 新 Session 加载目标、偏好和未完成话题
```

---

# 9. Loop 的四层实现顺序

## Loop 1：单轮状态循环

```text
用户消息
→ State Tracker 生成 StateDelta
→ State Reducer 更新 SessionState
→ Strategy Planner 使用新状态
→ Response Agent 回复
```

验收：

- 用户改变目标后，Session State 更新；
- 用户纠正后，旧状态不再继续生效；
- 用户拒绝某种对话方式后，偏好被记录；
- State Reducer 不修改旧对象。

---

## Loop 2：策略反馈循环

```text
Assistant 第 t 轮使用 Strategy
→ 创建 Pending Intervention
→ User 第 t+1 轮回复
→ Feedback Evaluator 评估
→ Strategy Planner 继续或切换
```

验收：

- 显式负面反馈可以触发策略切换；
- 不把“用户继续聊天”当作成功；
- 不连续重复已被拒绝的策略；
- Feedback 只描述可观察反应。

---

## Loop 3：长对话循环

```text
消息逐渐增加
→ Rolling Summarizer 压缩旧消息
→ Context Builder 组合：
   - Current Session State
   - Rolling Summary
   - Recent Messages
   - Current Strategy
```

验收：

- 摘要覆盖范围可追踪；
- 不重复摘要同一批消息；
- 用户原话优先于旧摘要；
- 摘要冲突不会覆盖当前状态；
- 摘要错误可以重建。

---

## Loop 4：跨会话循环

```text
Session 结束
→ Session Finalizer
→ Memory Candidate
→ Memory Policy
→ Long-term Memory
→ New Session Bootstrap
```

第一版只记：

- 用户确认的长期目标；
- 用户明确的交流偏好；
- 上次未完成话题；
- 明确表示有效或无效的方法。

禁止直接建立：

- 人格画像；
- 心理诊断；
- 未确认的模型推断；
- 基于一次互动形成的稳定 Procedural Memory。

---

# 10. 集成门槛

## Gate 1：Contract Gate

必须满足：

- 所有公共 Schema 可导入；
- Pydantic 校验通过；
- Agent 接口一致；
- Repository 接口一致；
- 无循环依赖。

## Gate 2：Fake Pipeline Gate

必须满足：

```text
POST /chat/turn
→ 完整 Fake 链路
→ 返回结果
```

并通过：

```bash
ruff check .
mypy .
pytest
```

## Gate 3：Persistence Gate

必须满足：

- 用户消息落库；
- Assistant 回复落库；
- Session State 版本化；
- Pending Intervention 可创建和完成。

## Gate 4：Adaptive Loop Gate

必须满足：

- 上一轮策略可被记录；
- 当前用户回复可被解释为反馈；
- Strategy Planner 可根据反馈调整；
- State 和 Context 正确更新；
- Loop 有固定 E2E 场景。

## Gate 5：Cross-Session Gate

必须满足：

- Session Summary 可生成；
- 长期记忆需通过 Policy；
- 新 Session 可读取上次未完成话题；
- 用户可以删除长期记忆。

---

# 11. Codex 使用规则

每个人给 Codex 的任务必须包含：

```text
1. Read AGENTS.md.
2. Read docs/system_design.md.
3. Read docs/code_architecture.md.
4. Read docs/team_collaboration_plan.md.
5. Read relevant contract documents.
6. Work only within the assigned files.
7. Do not modify public schemas unless explicitly authorized.
8. Add or update tests.
9. Run ruff, mypy, and pytest.
10. Report changed files and limitations.
```

禁止给 Codex：

```text
Implement the whole project.
```

推荐：

```text
Implement GitHub Issue #12.

Allowed files:
- services/state_reducer.py
- tests/unit/test_state_reducer.py

Do not modify:
- schemas/
- orchestrator/
- storage/

Acceptance criteria:
- no mutation of previous state;
- conflicts are marked rather than overwritten;
- version increments once;
- all tests pass.
```

---

# 12. 四个 Codex 的第一条任务

## 人员 A 的 Codex

```text
Read AGENTS.md and all design documents.

Implement only:
- shared Pydantic schemas for the first single-turn pipeline;
- Agent and Repository Protocols;
- Fake TurnOrchestrator;
- deterministic StateReducer interface;
- ContextBuilder interface;
- tests/integration/test_turn_flow.py.

Do not implement database access or real LLM calls.
Do not modify storage internals.
```

## 人员 B 的 Codex

```text
Read AGENTS.md and the database-related design sections.

Implement:
- async SQLAlchemy database foundation;
- users, sessions, messages, session_state_versions,
  intervention_events;
- repository implementations;
- basic health, chat, and session API.

Do not implement business logic.
Do not modify agent files or public schemas.
```

## 人员 C 的 Codex

```text
Read AGENTS.md and the Agent contracts.

Implement:
- LLMClient;
- structured output validation;
- retry and timeout handling;
- Fake RiskAgent;
- Fake StateTracker;
- Fake StrategyPlanner;
- Fake ResponseAgent;
- Fake OutputGuard;
- contract tests.

Do not modify storage or orchestrator files.
```

## 人员 D 的 Codex

```text
Read AGENTS.md and the collaboration plan.

Implement:
- FastAPI application startup and lifecycle;
- CI workflow;
- test fixtures;
- contract test harness;
- evaluation runner skeleton;
- audit logging interface.

Do not modify public schemas.
Do not modify the TurnOrchestrator main flow.
Do not implement dialogue business logic.
```

---

# 13. 每日协作节奏

建议每天至少两次同步。

## 上午

- 同步公共 Contract 是否有变更；
- 明确每个人当天 Issue；
- 确认文件范围；
- 确认是否有跨模块依赖。

## 晚上

- 每人提交一个小 PR 或可运行 commit；
- 运行 CI；
- 人员 A 负责主链路合并；
- 人员 D 运行完整回归；
- 记录接口问题；
- 更新第二天任务。

不要连续两天不集成。

---

# 14. Pull Request 规范

每个 PR 必须说明：

```text
Goal
Files changed
Public interface impact
Database migration impact
Tests added
Commands run
Known limitations
Follow-up issues
```

PR 应尽量：

- 小于 500 行核心逻辑变更；
- 一次只解决一个 Issue；
- 不夹带无关重构；
- 不同时大改 Schema 和实现；
- 公共 Contract 修改单独提交。

---

# 15. 冲突处理

## Schema 冲突

暂停业务开发，先提交 Contract PR，由人员 A 决定。

## Orchestrator 冲突

由人员 A 统一处理，不允许各模块自行复制一套流程。

## Database 字段冲突

人员 B 提供 migration proposal，人员 A 和对应消费者确认后合并。

## Prompt 与 Schema 不一致

以 Schema 为准，人员 C 修改 Prompt 或 Structured Output。

## State 和 Memory 语义冲突

人员 A 给出 Policy，人员 D 增加对应测试与评测。

## 运行环境冲突

人员 D 负责复现、记录和修复基础设施问题。

---

# 16. 第一版明确不做

为了保证 7 天内完成 Demo，暂时不做：

- 在线模型微调；
- 自动更新模型权重；
- 复杂知识图谱；
- 多模态 AU/心率/gaze；
- 临床诊断；
- 药物建议；
- 完整真人接管平台；
- 多 Agent 自由讨论；
- 复杂权限系统；
- 大规模 RAG；
- 多模型自动竞赛式路由。

---

# 17. 第一版验收标准

## 功能

- 支持多轮对话；
- Session State 可更新；
- Context 可构建；
- 策略可选择；
- 用户反馈可影响下一轮策略；
- 长对话可滚动摘要；
- Session 结束可生成摘要；
- 新 Session 可读取少量长期状态；
- 用户可以查看和删除记忆。

## 工程

- FastAPI 可启动；
- PostgreSQL 可迁移；
- 所有 Agent 有结构化输出；
- CI 运行 ruff、mypy、pytest；
- README 有启动方式；
- 有 3–5 个固定 Demo 场景；
- 有至少一条跨会话 E2E 测试；
- 有可重复运行的 Evaluation Runner。

## 安全

- Risk Agent 可路由；
- Response Agent 不直接修改风险状态；
- Output Guard 可阻止越界输出；
- 不提交真实用户数据；
- 不自动写入未经 Policy 审核的长期记忆。

---

# 18. 简历贡献划分

## 人员 A

强调：

- multi-agent orchestration；
- contract-driven architecture；
- adaptive dialogue loop；
- rolling summarization；
- cross-session memory；
- end-to-end system integration。

## 人员 B

强调：

- async FastAPI backend；
- PostgreSQL；
- versioned state persistence；
- repository pattern；
- API and database design。

## 人员 C

强调：

- LLM agent framework；
- structured outputs；
- prompt engineering；
- safety routing；
- local model serving。

## 人员 D

强调：

- CI/CD；
- evaluation framework；
- integration testing；
- auditability；
- containerized deployment；
- system reliability。

---

# 19. 最终原则

```text
公共 Contract 先行
人员 A 先搭薄底座
人员 A 随后主做 Adaptive Loop
人员 A 保留 Orchestrator 和 Contract 所有权
人员 B 实现数据与 API
人员 C 实现模型与在线 Agent
人员 D 实现平台、评测与集成支持
每天集成
逐步替换 Fake 模块
```

本项目不是四个人各自生成一套完整系统。

正确方式是：

```text
A 控制架构并实现核心 Loop
B 提供持久化与 API
C 提供模型能力
D 保证系统可运行、可测试、可评测、可部署
```
