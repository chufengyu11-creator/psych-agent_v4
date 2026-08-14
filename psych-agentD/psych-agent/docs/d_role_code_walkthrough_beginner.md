# D 角色当前代码的全量只读审计与外行版讲解

> 审计日期：2026-07-16  
> 唯一事实来源：当前 VS Code 工作目录 `D:\陈若涵\715\psych-agent\psych-agent` 中的实际代码。  
> 审计方式：只读检查代码、文档、测试、smoke、import、依赖注入和调用关系；没有参考旧压缩包。  
> 边界：本次只新增本文档，没有修改业务代码、schema、数据库、prompt、测试或配置。

## 审计前提：Git 能证明什么，不能证明什么

- 当前分支名是 `main`，但 `git log` 报告：`main` 还没有任何 commit。
- `git status --short` 显示当前项目文件全部是 untracked。
- 因为没有 commit，`git diff --name-status` 和 `git diff --stat` 都没有可比较内容，也不存在可用的 `merge-base`。
- 因此，Git **无法确认某个文件是谁修改的，也无法识别“D 相对主分支改了哪些文件”**。本文中“D 核心文件”只表示它属于 D 任务的代码职责，不表示 Git 已证明作者是 D。
- 指定文档中，`AGENTS.md`、`CONTRIBUTING.md`、`docs/system_design.md`、`docs/code_architecture.md` 当前都是 0 字节空文件。

# 1. 项目和 D 角色到底在做什么

## 1.1 整个系统在做什么

这套程序可以想成一个“带工作台和档案规则的心理支持聊天助手”。它不只是生成一句听起来温柔的话，还会保存原始消息、记录当前会话理解、观察上一轮沟通策略是否合适、生成摘要，并在用户允许时提出少量跨会话记忆。

它不是心理治疗、诊断或药物建议系统。程序设计的重点是：模型可以帮助理解和生成候选结果，但流程控制、数据校验、数据库写入和安全边界由普通程序掌握。

## 1.2 D 角色负责什么

本审计采用 `docs/detailed_task_breakdown.md` 中的 D 定义：D 负责反馈循环、长对话摘要、关闭会话总结、长期记忆候选、记忆政策和相关 worker/runtime 接入。通俗地说，D 负责让系统具备三种“回头看”的能力：

1. 看上一轮方法是否被用户接受；
2. 把本次长对话压缩成可追溯摘要；
3. 会话结束时，只把允许保存的信息变成长期记忆。

`psych_agent_team_collaboration_plan.md` 采用了另一版分工，把核心 loop 归给 A，把 worker、评测和集成支持归给 D。两份说明文档对“D”定义不一致。当前代码无 Git 作者历史，所以本文无法确认实际人员归属，只审计上述功能链路。

## 1.3 D 不负责什么

D 不负责自由修改公共 schema，不负责让 agent 直接写数据库，不负责风险诊断，不负责最终自然语言回复的全部质量，也不负责绕过 `MemoryPolicy` 永久保存模型推断。数据库表和 repository 的实现主要是存储层职责；D 只通过它们读写。

## 1.4 几个核心名词

- **session（会话）**：一次连续聊天，像一次有开始和结束的谈话。
- **summary（摘要）**：把本次会话较长的历史压缩成短版笔记；它是派生信息，不是原始事实。
- **memory（长期记忆）**：跨 session 仍可能使用的信息，例如用户明确说“每次只给我一个小步骤”。
- **fake（规则版）**：不用真实模型，靠固定规则稳定地产生结果，适合测试和安全回退。
- **model-backed（模型版）**：通过 `StructuredLLMClient` 请求模型产生符合 schema 的 JSON。
- **fallback（回退）**：模型失败或结果不可信时，改用 fake 规则版，避免整条流程崩溃。
- **schema（数据合同）**：规定一份数据必须有哪些字段、字段是什么类型，像统一表格模板。
- **agent（任务处理器）**：完成一个语义任务的组件，例如生成摘要；它不应直接控制数据库。
- **service（规则服务）**：普通、确定性的业务规则，例如判断一条候选记忆是否允许保存。
- **worker（流程工人）**：负责从 repository 收集数据、调用 agent/service、再交给 repository 保存。
- **repository（数据仓库接口）**：数据库访问的统一柜台，上层不用知道 SQLAlchemy 表怎么写。
- **runtime（运行时组装）**：把 agent、worker、repository 和事务真正接在一起的代码。
- **API（网络入口）**：把 HTTP 请求转换成 Python 方法调用；当前 API 主要用于本地调试和 demo。

# 2. D 角色完整调用流程

## 2.1 流程一：每轮对话后的自动摘要

### ASCII 流程图

```text
用户发送消息
  -> TurnOrchestrator.handle_turn
  -> TaskQueue.enqueue("post_turn")
  -> InlinePostTurnTaskQueue.enqueue
  -> PostTurnWorker.handle_post_turn
  -> SummaryWorker.handle_summary_requested
  -> FakeRollingSummarizer.summarize（默认）
     或 RollingSummarizer.summarize（显式注入时）
  -> SummaryRepository.save_version
  -> rolling_summary_versions
```

### 每一步是什么

| 步骤 | 一句话说明 | 文件 | 实际类 / 方法 |
|---|---|---|---|
| 接收一轮消息 | 控制保存消息、状态、策略和回复的顺序 | `orchestrator/turn_orchestrator.py:L88-L176` | `TurnOrchestrator.handle_turn` |
| 发出任务 | 普通回复保存后调用名为 `post_turn` 的任务 | `orchestrator/turn_orchestrator.py:L163-L168` | `TaskQueue.enqueue` |
| 立即执行 | 当前实现不是后台队列，而是在 API 请求内马上执行 | `orchestrator/post_turn_pipeline.py:L25-L43` | `InlinePostTurnTaskQueue.enqueue` |
| 翻译事件 | 把 post-turn 请求翻译成摘要请求 | `workers/post_turn_worker.py:L30-L74` | `PostTurnWorker.handle_post_turn` |
| 组装摘要输入 | 读取旧摘要、最近消息、状态和干预记录 | `workers/summary_worker.py:L20-L89` | `SummaryWorker.handle_summary_requested` |
| 生成摘要 | 默认 SQLAlchemy runtime 使用 fake；可显式注入模型版 | `agents/rolling_summarizer.py:L17-L158` | `FakeRollingSummarizer.summarize` / `RollingSummarizer.summarize` |
| 保存版本 | 新增一行摘要版本并更新 session 当前版本号 | `storage/repositories/summary_repository.py:L45-L85` | `SqlAlchemySummaryRepository.save_version` |

重要限定：只有 `runtime_mode=sqlalchemy` 使用 `build_sqlalchemy_orchestrator_with_post_turn_summary` 时自动摘要。默认 `in_memory` runtime 使用 `NoopTaskQueue`，不生成摘要。SQLAlchemy 普通对话轮次当前每轮都摘要；安全路由在 enqueue 之前提前返回，因此安全路径不会执行这条 post-turn 摘要链。

## 2.2 流程二：关闭会话并保存记忆

### ASCII 流程图

```text
关闭 session
  -> SqlAlchemySessionCloser.close_session
  -> SummaryWorker.handle_summary_requested
  -> SessionCloseMemoryWorker.handle_session_close_requested
  -> SessionFinalizer.finalize（runtime 默认实际是 FakeSessionFinalizer）
  -> MemoryCurator.curate
  -> MemoryPolicy.evaluate_candidate
  -> ConflictResolver.detect
  -> MemoryRepository.create
  -> long_term_memories
  -> SessionRepository.close
```

### 每一步是什么

| 步骤 | 一句话说明 | 文件 | 实际类 / 方法 |
|---|---|---|---|
| 开启事务 | 把摘要、记忆和关闭状态放在同一个数据库事务里 | `runtime/sqlalchemy_session_closer.py:L56-L111` | `SqlAlchemySessionCloser.close_session` |
| 确保摘要 | 默认先把尚未覆盖的消息摘要掉 | `runtime/sqlalchemy_session_closer.py:L76-L90` | `SummaryWorker.handle_summary_requested` |
| 收集关闭材料 | 读取最近消息、最终状态、干预和摘要 | `workers/session_close_worker.py:L81-L106` | `SessionCloseMemoryWorker.handle_session_close_requested` |
| 生成总结 | 产出 `SessionFinalizerResult` 和候选记忆 | `agents/session_finalizer.py:L51-L160` | `FakeSessionFinalizer.finalize` / `SessionFinalizer.finalize` |
| 整理候选 | 合并 finalizer、用户原话和状态偏好中的候选，过滤假来源 | `agents/memory_curator.py:L40-L69` | `MemoryCurator.curate` |
| 政策审批 | 检查授权、来源、敏感度、模型推断和冲突 | `services/memory_policy.py:L70-L109` | `MemoryPolicy.evaluate_candidate` |
| 检查新旧关系 | 判断重复、强化、冲突或替代 | `services/conflict_resolver.py:L73-L92` | `ConflictResolver.detect` |
| 执行写入 | 只执行 policy 给出的操作，保存来源 ID | `storage/repositories/memory_repository.py:L104-L196` | `SqlAlchemyMemoryRepository.create` |
| 关闭会话 | 将 session 标成 `closed` 并记录结束时间 | `storage/repositories/session_repository.py:L135-L177` | `SqlAlchemySessionRepository.close` |

重要限定：`SqlAlchemySessionCloser` 把 `user_memory_enabled=True` 写死，没有读取 `users.memory_enabled`。所以数据库里即使用户默认是 `False`，关闭流程仍按开启记忆处理。

## 2.3 流程三：API 调用

### ASCII 流程图

```text
POST /chat/turn
  -> api.routers.chat.create_chat_turn
  -> app.dependencies.get_orchestrator
  -> in_memory: get_shared_orchestrator
     或 sqlalchemy: build_sqlalchemy_orchestrator_with_post_turn_summary
  -> handle_turn

POST /sessions/close
  -> api.routers.sessions.close_session
  -> app.dependencies.get_session_closer
  -> SqlAlchemySessionCloser.close_session
  -> session close memory 流程
```

### 每一步是什么

| 入口 | 一句话说明 | 文件 | 实际类 / 方法 |
|---|---|---|---|
| `POST /chat/turn` | 转换请求字段并调用 runtime | `api/routers/chat.py:L17-L29` | `create_chat_turn` |
| 选择聊天 runtime | `RUNTIME_MODE` 为 `in_memory` 或 `sqlalchemy` | `app/dependencies.py:L50-L63` | `get_orchestrator` |
| in-memory 模式 | 使用内存 repository、fake agents、`NoopTaskQueue` | `runtime/factory.py:L57-L80` | `build_in_memory_orchestrator` |
| SQLAlchemy 模式 | 每轮一个事务，并接入 inline 摘要 | `runtime/factory.py:L124-L150` | `build_sqlalchemy_orchestrator_with_post_turn_summary` |
| `POST /sessions/close` | 当前真实路径不是设计文档中的带路径参数版本 | `api/routers/sessions.py:L32-L49` | `close_session` |
| 选择关闭 runtime | 只允许 SQLAlchemy，in-memory 返回 HTTP 501 | `app/dependencies.py:L66-L77` | `get_session_closer` |

# 3. D 角色文件总表

## 3.1 核心实现与直接依赖

表中“D 直接修改”全部写“无法确认”，原因是 Git 没有 commit。括号中的“核心/支撑”是本次审计分类，不是作者归因。

| 文件路径 | 层 | D 直接修改 | 实际接口与作用 | 输入 -> 输出 | 谁调用它 -> 它调用谁 | DB | 模型 | fallback | 相关测试 |
|---|---|---|---|---|---|---|---|---|---|
| `agents/feedback_evaluator.py` | agent | 无法确认（核心） | 只有 `FakeFeedbackEvaluator.evaluate`，按关键词判断显式拒绝 | `FeedbackInput -> FeedbackResult` | Orchestrator -> 无外部依赖 | 否 | 否 | 不适用 | `test_turn_flow.py` |
| `prompts/feedback_evaluator.md` | prompt | 无法确认（核心） | **空文件** | 无 | 当前无人读取 | 否 | 否 | 否 | 无直接测试 |
| `schemas/feedback.py` | schema | 无法确认（公共支撑） | 定义反馈枚举、输入和结果 | Pydantic 合同 | agent/orchestrator/repository 使用 | 否 | 否 | 否 | contract、turn flow |
| `schemas/intervention.py` | schema | 无法确认（公共支撑） | 定义策略记录生命周期 | 字段 -> `InterventionRecord` | repository、feedback、summary 使用 | 否 | 否 | 否 | contract、model tests |
| `agents/rolling_summarizer.py` | agent | 无法确认（核心） | fake 摘要和模型摘要，校验来源，程序强制覆盖边界 | `RollingSummarizerInput -> RollingSummary` | SummaryWorker -> StructuredLLMClient/fake | 否 | 可选 | 有 | `test_rolling_summarizer.py` |
| `prompts/rolling_summarizer.md` | prompt | 无法确认（核心） | 约束模型只输出 JSON、不得编造 | prompt 文本 | RollingSummarizer 读取 | 否 | 间接 | 文件空时有内置短 prompt | summarizer tests |
| `workers/summary_worker.py` | worker | 无法确认（核心） | 收集未覆盖消息并保存新摘要版本 | event -> summary 或旧摘要 | PostTurnWorker/Closer -> repositories/summarizer | 是，经 repo | 间接 | 取决于注入 summarizer | unit/integration summary worker |
| `schemas/summary.py` | schema | 无法确认（公共支撑） | 定义摘要和关闭会话合同 | 字段 -> typed models | agents/workers/repos 使用 | 否 | 否 | 否 | contract、summarizer/finalizer tests |
| `schemas/events.py` | schema | 无法确认（公共支撑） | 定义 post-turn、摘要、关闭和候选事件 | 字段 -> event | orchestrator/workers 使用 | 否 | 否 | 否 | contract/worker tests |
| `storage/repositories/summary_repository.py` | repository | 无法确认（存储支撑） | 读当前摘要、写不可变版本 | session/summary -> summary/None | worker/context -> ORM | 是 | 否 | 否 | SQL summary repository |
| `storage/repositories/message_repository.py` | repository | 无法确认（存储支撑） | 写原始消息，取最近 N 条/按 ID 取 | IDs/text -> Message/list | orchestrator/workers -> ORM | 是/内存 | 否 | 否 | repository/worker/integration |
| `storage/repositories/state_repository.py` | repository | 无法确认（存储支撑） | 读写版本化 `SessionState` | session/state -> state/None | orchestrator/workers -> ORM | 是/内存 | 否 | 否 | repository/turn tests |
| `storage/repositories/intervention_repository.py` | repository | 无法确认（存储支撑） | 保存 pending/evaluated 策略记录 | typed args -> record/None | orchestrator/workers -> ORM | 是/内存 | 否 | 否 | turn/worker tests |
| `agents/session_finalizer.py` | agent | 无法确认（核心） | fake/模型版关闭总结，检查所有嵌套来源 ID | `SessionFinalizerInput -> SessionFinalizerResult` | CloseWorker -> StructuredLLMClient/fake | 否 | 可选 | 有 | `test_session_finalizer.py` |
| `prompts/session_finalizer.md` | prompt | 无法确认（核心） | 约束候选必须来自明确表达 | prompt 文本 | SessionFinalizer 读取 | 否 | 间接 | 文件空时有内置短 prompt | finalizer tests |
| `workers/session_close_worker.py` | worker | 无法确认（核心） | finalizer、curator、policy、repository、close 的总流程 | event -> `SessionCloseResult` | SqlAlchemySessionCloser -> 多个 repo/service | 是，经 repo | 间接 | 取决于注入 finalizer | unit/integration close worker |
| `runtime/sqlalchemy_session_closer.py` | runtime | 无法确认（核心接入） | 在一个事务中摘要、写记忆、关闭 session | IDs/reason -> close result | API/factory -> workers/repos | 是 | 默认否 | 默认 fake | session closer/API integration |
| `agents/memory_curator.py` | agent/规则 | 无法确认（核心） | 合并、过滤、去重少量明确偏好候选 | `MemoryCuratorInput -> list[MemoryCandidate]` | CloseWorker -> 本地规则 | 否 | 否 | 不适用 | `test_memory_curator.py` |
| `prompts/memory_curator.md` | prompt | 无法确认（核心） | **空文件；当前 MemoryCurator 不读它** | 无 | 当前无人读取 | 否 | 否 | 否 | 无直接测试 |
| `services/memory_policy.py` | service | 无法确认（核心） | 决定拒绝、创建、强化、替代或待确认 | `MemoryPolicyInput -> MemoryPolicyDecision` | CloseWorker -> ConflictResolver | 否 | 否 | 不适用 | `test_memory_policy.py` |
| `services/conflict_resolver.py` | service | 无法确认（核心） | 规则判断重复、强化、冲突、替代 | candidate + existing -> conflicts | MemoryPolicy -> 本地文本规则 | 否 | 否 | 不适用 | `test_memory_policy.py` |
| `services/memory_retriever.py` | service | 无法确认（关键缺口） | 当前始终返回空 `RetrievedMemories` | user/query/state -> 空结果 | Orchestrator -> 无 | 否 | 否 | 不适用 | context/turn fixtures，未验证 DB 检索 |
| `schemas/memory.py` | schema | 无法确认（公共支撑） | 定义长期记忆、候选、policy 和写入结果 | 字段 -> typed models | memory 全链路使用 | 否 | 否 | 否 | contract/model/repository tests |
| `storage/repositories/memory_repository.py` | repository | 无法确认（存储支撑） | 执行 policy 已决定的 create/reinforce/supersede | candidate+decision -> write result | CloseWorker -> ORM | 是/内存 | 否 | 否 | SQL memory repository/close tests |
| `workers/post_turn_worker.py` | worker | 无法确认（核心） | 把 post_turn 翻译成摘要事件 | IDs -> `PostTurnWorkerResult` | InlineQueue -> SummaryWorker | 间接 | 间接 | 取决于下游 | unit/integration post-turn |
| `orchestrator/post_turn_pipeline.py` | queue 边界 | 无法确认（核心接入） | Noop 或同步 inline 执行；不是真后台队列 | task name/IDs -> None | Orchestrator -> PostTurnWorker | 间接 | 间接 | 否 | queue/runtime tests |
| `orchestrator/turn_orchestrator.py` | orchestrator | 无法确认（公共接入） | 普通轮次结束时 enqueue；反馈在下一轮评估 | IDs/text -> ChatTurnResult | API/runtime -> agents/services/repos | 是，经 repo | 视 agent | 各 agent 自己处理 | turn/post-turn integration |
| `runtime/sqlalchemy_orchestrator.py` | runtime | 无法确认（接入） | 每轮开启事务并绑定 SQL repo；内层 agents 仍是 fake | IDs/text -> ChatTurnResult | factory/API -> TurnOrchestrator | 是 | 默认否 | fake | runtime task queue tests |
| `runtime/factory.py` | runtime factory | 无法确认（接入） | 组装 in-memory、SQL summary、session closer | dependencies -> runtime | app dependencies/scripts | 间接 | 部分工厂可用模型 | 默认 fake | runtime/API integration |
| `app/config.py` | config | 无法确认（接入） | `runtime_mode` 默认 `in_memory`，保存 DB/LLM 配置 | env -> Settings | dependencies 使用 | 否 | 否 | 默认值 | API config tests |
| `app/dependencies.py` | dependency | 无法确认（接入） | 按配置缓存 runtime/engine/closer | Settings -> cached objects | FastAPI -> factory | 间接 | 否 | 否 | API runtime tests |
| `app/main.py` | API app | 无法确认（接入） | 注册 health/chat/sessions；未注册 memories | 无 -> FastAPI | uvicorn/tests -> routers | 否 | 否 | 否 | API tests |
| `api/routers/chat.py` | API | 无法确认（接入） | `POST /chat/turn` 调 `handle_turn` | request -> response | HTTP -> runtime | 间接 | 间接 | 否 | API runtime tests |
| `api/routers/sessions.py` | API | 无法确认（接入） | `POST /sessions/close` 调 closer | close request -> aggregate response | HTTP -> session closer | 间接 | 默认否 | runtime 默认 fake | API runtime tests |
| `api/routers/memories.py` | API | 无法确认（缺口） | **空文件，未注册路由** | 无 | 无 | 否 | 否 | 否 | 无 |
| `storage/repositories/session_repository.py` | repository | 无法确认（支撑） | 所有权检查和幂等 close | IDs -> None/bool | runtime/worker -> ORM | 是 | 否 | 否 | user/session repository tests |

## 3.2 数据表模型文件

| 文件 | 层 | D 直接修改 | 作用 / 输入输出 | 调用关系 | DB | 模型 | fallback | 相关测试 |
|---|---|---|---|---|---|---|---|---|
| `storage/models/user.py` | ORM | 无法确认 | `users`，含 `memory_enabled`，默认 False | UserRepository 使用 | 是 | 否 | 否 | user/session models/repos |
| `storage/models/session.py` | ORM | 无法确认 | `sessions`，保存状态/摘要版本计数和 close 状态 | session/summary/state repo 使用 | 是 | 否 | 否 | models/repos/closer |
| `storage/models/message.py` | ORM | 无法确认 | `messages` 原始消息和顺序 | MessageRepository 使用 | 是 | 否 | 否 | core models/repositories |
| `storage/models/session_state.py` | ORM | 无法确认 | `session_state_versions` 不可变快照 | StateRepository 使用 | 是 | 否 | 否 | core models/turn flow |
| `storage/models/intervention.py` | ORM | 无法确认 | `intervention_events` 策略与后续反馈 | InterventionRepository 使用 | 是 | 否 | 否 | core models/turn flow |
| `storage/models/summary.py` | ORM | 无法确认 | `rolling_summary_versions` 摘要快照及覆盖边界 | SummaryRepository 使用 | 是 | 否 | 否 | summary model/repo |
| `storage/models/memory.py` | ORM | 无法确认 | `long_term_memories`，含来源、确认和生命周期字段 | MemoryRepository 使用 | 是 | 否 | 否 | memory model/repo |

## 3.3 测试、fixture 与 smoke 清单

这些文件不被生产代码调用；它们消费生产接口，输出断言结果或 smoke 日志，不直接调用真实 Qwen。

| 文件 | 层 | D 直接修改 | 主要验证 | 输入 -> 输出 | DB | 模型 | fallback | 关联实现 |
|---|---|---|---|---|---|---|---|---|
| `tests/contract/test_loop_memory_contracts.py` | contract test | 无法确认 | summary/finalizer/memory/event schema 可构造 | fixture -> assertions | 否 | 否 | 否 | schemas |
| `tests/fixtures/interventions.py` | fixture | 无法确认 | typed intervention 样本 | 无 -> records | 否 | 否 | 否 | feedback/summary |
| `tests/fixtures/memories.py` | fixture | 无法确认 | typed memory 样本 | 无 -> memories | 否 | 否 | 否 | memory/context |
| `tests/fixtures/summaries.py` | fixture | 无法确认 | typed summary/input 样本 | 无 -> summaries | 否 | 否 | 否 | summarizer/context |
| `tests/unit/test_rolling_summarizer.py` | unit | 无法确认 | fake、模型 stub、非法来源和异常回退 | payload -> assertions | 否 | scripted stub | 有 | rolling summarizer |
| `tests/unit/test_session_finalizer.py` | unit | 无法确认 | fake、模型 stub、嵌套假来源回退 | payload -> assertions | 否 | scripted stub | 有 | finalizer |
| `tests/unit/test_memory_curator.py` | unit | 无法确认 | 用户/assistant 来源、过滤和去重 | payload -> assertions | 否 | 否 | 否 | curator |
| `tests/unit/test_memory_policy.py` | unit | 无法确认 | 授权、模型推断、重复和替代 | candidate -> decision assertions | 否 | 否 | 否 | policy/conflict |
| `tests/unit/test_summary_worker.py` | unit | 无法确认 | 生成、版本递增、幂等和模型注入 | event -> assertions | 内存 | scripted stub | 有 | summary worker |
| `tests/unit/test_post_turn_worker.py` | unit | 无法确认 | enable 开关和事件翻译 | IDs -> result assertions | 否 | 否 | 否 | post-turn worker |
| `tests/unit/test_session_close_worker.py` | unit | 无法确认 | policy 必经、来源保留、关闭调用 | event -> assertions | 内存 | scripted stub | 有 | close worker |
| `tests/unit/test_sqlalchemy_orchestrator_task_queue_factory.py` | unit | 无法确认 | queue/factory 互斥和默认 Noop | config -> assertions | 否 | 否 | 否 | SQL runtime |
| `tests/unit/test_context_builder.py` | unit | 无法确认 | summary 和 memory 能进入 context | fixtures -> assertions | 否 | 否 | 否 | context consumer |
| `tests/unit/test_summary_memory_models.py` | unit | 无法确认 | summary/memory 表字段约束 | metadata -> assertions | 编译 DDL | 否 | 否 | ORM models |
| `tests/unit/test_core_conversation_models.py` | unit | 无法确认 | message/state/intervention 表约束 | metadata -> assertions | 编译 DDL | 否 | 否 | ORM models |
| `tests/unit/test_user_session_models.py` | unit | 无法确认 | user/session 默认值和约束 | metadata -> assertions | 编译 DDL | 否 | 否 | ORM models |
| `tests/unit/test_user_session_repositories.py` | unit | 无法确认 | memory 默认关闭、close 幂等、事务 | calls -> assertions | SQLite | 否 | 否 | user/session repo |
| `tests/integration/test_summary_worker.py` | integration | 无法确认 | worker 真写 summary 表 | event -> DB assertions | SQLite | 否 | fake | summary worker/repo |
| `tests/integration/test_sqlalchemy_summary_repository.py` | integration | 无法确认 | 摘要持久化和 session 计数 | summary -> DB assertions | SQLite | 否 | 否 | summary repo |
| `tests/integration/test_sqlalchemy_memory_repository.py` | integration | 无法确认 | CREATE 和 REINFORCE | candidate/decision -> DB | SQLite | 否 | 否 | memory repo |
| `tests/integration/test_post_turn_task_queue.py` | integration | 无法确认 | inline queue 同步生成摘要 | task -> stored summary | 内存 | 否 | fake | inline queue |
| `tests/integration/test_sqlalchemy_post_turn_summary_flow.py` | integration | 无法确认 | Orchestrator enqueue 到摘要表 | turn -> DB | SQLite | 否 | fake | full post-turn |
| `tests/integration/test_sqlalchemy_post_turn_runtime_factory.py` | integration | 无法确认 | factory 每普通轮接入摘要 | turns -> DB | SQLite | 否 | fake | runtime factory |
| `tests/integration/test_session_close_worker.py` | integration | 无法确认 | 来源写入并关闭 session | close event -> DB | SQLite | 否 | fake | close worker |
| `tests/integration/test_sqlalchemy_session_closer.py` | integration | 无法确认 | 同事务 summary/memory/close | close -> DB | SQLite | 否 | fake | session closer |
| `tests/integration/test_api_sqlalchemy_runtime.py` | integration | 无法确认 | runtime 模式、chat、close API | HTTP -> DB/response | SQLite | 否 | fake | app/API/runtime |
| `tests/integration/test_turn_flow.py` | integration | 无法确认 | fake feedback 影响第二轮策略 | turns -> response | 内存 | 否 | fake | feedback loop |
| `scripts/smoke_post_turn_summary_worker.py` | smoke | 无法确认 | 三轮 inline 摘要 | turns -> SQLite/log | SQLite | 否 | fake | post-turn summary |
| `scripts/smoke_session_close_memory.py` | smoke | 无法确认 | 关闭、记忆和表内容 | scripted turns -> SQLite/log | SQLite | scripted client | finalizer 默认 fake | close flow |
| `scripts/smoke_model_assisted_database_corpus.py` | smoke | 无法确认 | structured agent/summary 适配 | scripted JSON -> SQLite/log | SQLite | **scripted，不是真 Qwen** | 有 | model-assisted flow |
| `scripts/smoke_api_sqlalchemy_runtime.py` | smoke | 无法确认 | chat + close API 持久化 | HTTP -> SQLite/log | SQLite | 否 | fake | API runtime |

另外，`workers/worker_app.py`、`workers/memory_worker.py`、`workers/evaluation_worker.py` 当前均为空。它们证明仓库还没有真正的后台 worker 应用或独立 memory worker。

# 4. 按文件逐个讲解

本节把“D 核心实现文件”定义为直接承载反馈、摘要、关闭、记忆政策和触发流程的文件。这个定义不是 Git 作者归因。

## 4.1 `agents/feedback_evaluator.py:L12-L52`

1. 最简单的话：它看用户最新一句话有没有明确拒绝上一轮方法。  
2. 必要性：下一轮策略要知道上一轮是否不合适。  
3. 重要接口：只有 `FakeFeedbackEvaluator.run/evaluate`，不存在真实 `FeedbackEvaluator` 类。  
4. 参数：`FeedbackInput`，含上一轮 intervention、assistant 消息、下一条用户消息。  
5. 返回：`FeedbackResult`。  
6. 数据来源：Orchestrator 从 message/intervention repository 取出后组装。  
7. 结果去向：InterventionRepository 完成上一条记录，StateReducer 和 StrategyPlanner 使用。  
8. 正常顺序：取用户文本 -> 查找负面关键词 -> 返回 NEGATIVE 或 ABSENT。  
9. 出错：没有 try/fallback；它本身是简单规则版。  
10. 数据库：不访问。  
11. 长期记忆：不写。  
12. source ID：本文件不产生 source ID。  
13. 示例：“我不想继续分析”命中“不想”，返回 poor、not_achieved、建议切换。  
14. 限制：只看关键词、区分大小写不完整、没有 positive/mixed 规则、prompt 为空、没有模型版。  
15. 测试：`tests/integration/test_turn_flow.py` 验证第二轮明确拒绝能影响策略；没有独立 feedback 单元测试。

## 4.2 `agents/rolling_summarizer.py:L17-L294`

1. 最简单的话：把旧消息整理成一页可追溯笔记。  
2. 必要性：长对话不能无限把所有原文喂给后续模型。  
3. 重要接口：`FakeRollingSummarizer`、`RollingSummarizer` 和来源/覆盖边界辅助函数。  
4. 参数：`RollingSummarizerInput`；构造模型版时还可传 client、model_name、fallback、prompt。  
5. 返回：`RollingSummary`。  
6. 数据来源：SummaryWorker 提供旧摘要、未覆盖消息、当前状态和 interventions。  
7. 结果去向：SummaryWorker 交给 SummaryRepository。  
8. 正常顺序：fake 合并字段；模型版构造 JSON prompt -> structured client -> 校验来源 -> 程序覆盖 session/version/coverage。  
9. 出错：任何 client 异常或来源 ID 非法，整体回退 fake。  
10. 数据库：不访问。  
11. 长期记忆：不写。  
12. source ID：模型只能引用输入中真实 ID；`_has_valid_source_ids` 在 `L277-L281` 校验。  
13. 示例：用户说“会议前焦虑”，摘要可写入 important statement，并引用该 user message ID。  
14. 限制：fake 会把所有用户原话塞入列表；没有 token/长度控制；不做定期从原文重建；只校验 ID，不校验每段文字与具体 ID 的语义对应。  
15. 测试：验证 fake、模型 stub、本地覆盖边界、未知来源和异常 fallback。

## 4.3 `workers/summary_worker.py:L13-L92`

1. 最简单的话：它是摘要流程的“材料员兼保存员”。  
2. 必要性：summarizer 不应自己查数据库或写数据库。  
3. 重要接口：`Summarizer` Protocol、`SummaryWorker`。  
4. 参数：四个 repository、summarizer、`message_limit`；处理方法接收 `RollingSummaryRequestedEvent`。  
5. 返回：新摘要、旧摘要或 None。  
6. 数据来源：repository 读取旧摘要、最近消息、状态和 interventions。  
7. 结果去向：`SummaryRepository.save_version`。  
8. 正常顺序：取旧摘要 -> 取最近消息 -> 应用 after boundary -> 排除已覆盖消息 -> 组装 schema -> summarize -> save。  
9. 出错：没有本地捕获；summarizer fallback 可处理模型错误，repository 错误交给外层事务。  
10. 数据库：通过 repository 间接访问。  
11. 长期记忆：不写。  
12. source ID：随 `RollingSummary` 保存；worker 自己不验证语义。  
13. 示例：已有摘要覆盖到 msg_10，新请求只把 sequence > msg_10 的消息交给 summarizer。  
14. 限制：`get_recent(limit=50)` 先截最新 50 条，可能永久漏掉更老的未覆盖消息；after ID 不存在时不会报错；没有阈值，当前 SQL runtime 每普通轮触发。  
15. 测试：验证保存、版本递增、重复请求不重存、模型版可注入、SQL 表计数更新。

## 4.4 `workers/post_turn_worker.py:L12-L77` 与 `orchestrator/post_turn_pipeline.py:L9-L46`

1. 最简单的话：前者把“这一轮结束了”翻译成“请更新摘要”，后者决定不做或立即做。  
2. 必要性：让 Orchestrator 只发统一任务，不直接知道摘要细节。  
3. 重要接口：`PostTurnWorkerResult`、`SummaryWorkerProtocol`、`PostTurnWorker`、`TaskQueue`、`NoopTaskQueue`、`InlinePostTurnTaskQueue`。  
4. 参数：user/session ID、task name、enable_summary。  
5. 返回：worker 返回结果；queue 的 enqueue 返回 None 并记录结果。  
6. 数据来源：TurnOrchestrator 在保存普通回复和 pending intervention 后调用。  
7. 结果去向：SummaryWorker。  
8. 正常顺序：enqueue -> 检查任务名 -> handle_post_turn -> 创建摘要事件 -> worker。  
9. 出错：没有重试、死信或隔离；异常会冒泡并使当前事务/API 失败。  
10. 数据库：只通过下游 worker 间接访问。  
11. 长期记忆：不写。  
12. source ID：post-turn 方法甚至没有使用 user_id；摘要 worker自行取消息。  
13. 示例：SQL chat 成功生成回复后，同一个请求里马上生成 version 1 摘要。  
14. 限制：名字叫 queue，但 inline 实现不是后台；会增加 API 延迟；Noop 会完全丢弃任务。  
15. 测试：验证 disable、事件字段、None 结果、inline 落库和 runtime factory 接线。

## 4.5 `agents/session_finalizer.py:L51-L392`

1. 最简单的话：会话结束时写一份结构化“结案清单”，但不是医疗结论。  
2. 必要性：跨 session 记忆需要从完整会话中先提出候选。  
3. 重要接口：`FakeSessionFinalizer`、`SessionFinalizer` 及 action/memory/outcome 提取函数。  
4. 参数：`SessionFinalizerInput`；模型版构造参数类似 summarizer。  
5. 返回：`SessionFinalizerResult`。  
6. 数据来源：CloseWorker 提供最近消息、最终状态、interventions 和摘要。  
7. 结果去向：MemoryCurator，再到 MemoryPolicy。  
8. 正常顺序：fake 按规则提取；模型版调用 structured client -> 校验顶层和嵌套 source IDs -> 强制 session ID。  
9. 出错：模型异常或任何嵌套来源非法，整体 fallback fake。  
10. 数据库：不访问。  
11. 长期记忆：只生成候选，不写。  
12. source ID：`L173-L216` 收集允许 ID 并检查 action、candidate、outcome 的全部嵌套引用。  
13. 示例：“请每次一个小步骤”会产生低敏感、显式来源的 interaction preference candidate。  
14. 限制：fake 只识别很少的中英文短语；“具体下一步”生成英文固定 action；读取的消息最多 50 条；模型字段只做 schema/ID 校验，不做逐句事实蕴含验证。  
15. 测试：验证用户/assistant 区分、无 client fallback、模型 stub、异常和嵌套假 ID fallback。

## 4.6 `agents/memory_curator.py:L40-L169`

1. 最简单的话：把多处候选记忆合并、去掉没证据的、合并重复的。  
2. 必要性：finalizer 的候选不能直接相信，也可能漏掉规则能识别的明确偏好。  
3. 重要接口：`MemoryCuratorInput`、`MemoryCurator.curate`。  
4. 参数：session、messages、final_state、finalizer_result、可选 rolling_summary。  
5. 返回：`list[MemoryCandidate]`。  
6. 数据来源：CloseWorker。  
7. 结果去向：CloseWorker 替换 finalizer result 中的候选，再逐条交 policy。  
8. 正常顺序：收集允许来源 -> 合并三类候选 -> 过滤空/未知来源 -> 按类型和内容去重并合并来源。  
9. 出错：没有 fallback；纯规则一般直接抛出类型/程序错误。  
10. 数据库：不访问。  
11. 长期记忆：不写。  
12. source ID：候选必须至少有一个 ID，且全部位于输入允许集合。  
13. 示例：finalizer 和用户原话都生成同一“小步骤”偏好时，最终只保留一条并合并来源。  
14. 限制：当前只生成一种 interaction preference；把“想要具体下一步”统一改写成“每次一个小步骤”，语义可能被错误合并；空 prompt 未使用。  
15. 测试：验证保留、提取、assistant 排除、状态来源、未知来源过滤和去重。

## 4.7 `services/memory_policy.py:L70-L255`

1. 最简单的话：长期记忆的门卫。  
2. 必要性：候选不等于许可，尤其不能把诊断、人格判断或模型推测直接保存。  
3. 重要接口：`MemoryPolicy.evaluate_candidate` 和决定辅助函数。  
4. 参数：`MemoryPolicyInput`。  
5. 返回：`MemoryPolicyDecision`。  
6. 数据来源：CloseWorker 提供候选、现有 active memories、开关和 session ID。  
7. 结果去向：MemoryRepository。  
8. 正常顺序：检查开关/来源 -> 禁止内容 -> 模型推断 -> 冲突 -> 确认要求 -> 低风险显式偏好自动允许。  
9. 出错：纯规则无 fallback；输入先由 Pydantic 校验。  
10. 数据库：不访问。  
11. 长期记忆：不直接写，只做决定。  
12. source ID：只检查是否非空，不验证 ID 是否真实；真实性由前面的 agent/curator 负责。  
13. 示例：显式、低敏感、confidence 0.9 的小步骤偏好允许 CREATE；model_inference 被拒绝。  
14. 限制：敏感词黑名单很窄；confirmation 决定为 `allowed=True`，repository 会先写 pending 行，但没有 API 完成确认；`sanitized_content` 当前未设置也未使用。  
15. 测试：覆盖显式偏好、关闭开关、模型推断、重复强化和替代关系；未覆盖全部敏感/确认分支。

## 4.8 `services/conflict_resolver.py:L73-L275`

1. 最简单的话：比较新候选和旧记忆是不是同一件事、相反意思或更新说法。  
2. 必要性：避免重复写很多相同记忆，也避免新旧偏好互相打架。  
3. 重要接口：`ConflictResolver.detect`、`detect_memory_conflicts`。  
4. 参数：一条 candidate 和现有 `LongTermMemory` 列表。  
5. 返回：`list[MemoryConflict]`。  
6. 数据来源：MemoryPolicy。  
7. 结果去向：MemoryPolicy 转成 REINFORCE/SUPERSEDE/MARK_CONFLICT 决定。  
8. 正常顺序：文本标准化 -> 判断是否相关 -> duplicate -> supersede -> conflict -> reinforce。  
9. 出错：纯规则，无 fallback。  
10. 数据库：不访问。  
11. 长期记忆：不写。  
12. source ID：不参与冲突判断。  
13. 示例：旧偏好“小步骤”，新候选“现在想一次拿完整方案”被识别为 supersede。  
14. 限制：依赖少量中英文关键词；中文通用分词几乎没有；第一个 conflict 决定 policy，多个关系没有综合排序。  
15. 测试：当前直接测试 duplicate 和 supersede；其他分支覆盖不足。

## 4.9 `workers/session_close_worker.py:L20-L155`

1. 最简单的话：关闭会话时的总管。  
2. 必要性：保证 finalizer、curator、policy、repository 和 close 顺序固定。  
3. 重要接口：两个 Protocol、`SessionCloseResult`、`SessionCloseMemoryWorker`。  
4. 参数：六个 repository/agent/service 依赖、limit、memory 开关、可选 curator；处理方法接收 close event。  
5. 返回：候选数、写入数、每条写入结果和 finalizer result。  
6. 数据来源：repository。  
7. 结果去向：SqlAlchemySessionCloser/API。  
8. 正常顺序：验所有权 -> 读材料 -> finalize -> curate -> 每条读 active memories -> policy -> create -> close。  
9. 出错：没有内部吞错；外层事务会回滚。  
10. 数据库：全部通过 repository。  
11. 长期记忆：自己不写 ORM，但会调用 MemoryRepository。  
12. source ID：候选带入 repository，最终存入表。  
13. 示例：小步骤偏好经 policy 后写一行 active memory，然后 session closed。  
14. 限制：每条候选都重新 list_active；limit 50 可能漏历史；`user_memory_enabled` 由构造参数决定；重复 close 会重新跑 finalizer/policy，可能强化记忆。  
15. 测试：验证空候选也关闭、fake/模型/curator 候选都必经 policy、SQL 来源落库。

## 4.10 `runtime/sqlalchemy_session_closer.py:L30-L114`

1. 最简单的话：给关闭流程准备同一个数据库事务和所有工具。  
2. 必要性：任何一步失败时，摘要、记忆和关闭状态应一起回滚。  
3. 重要接口：`SqlAlchemySessionCloser.__init__/close_session`。  
4. 参数：session factory 和可替换 summarizer/finalizer/curator/policy。  
5. 返回：`SessionCloseResult`。  
6. 数据来源：SQLAlchemy repositories。  
7. 结果去向：sessions API。  
8. 正常顺序：开事务 -> ensure user/owner -> 可选摘要 -> 构造 close worker -> 执行。  
9. 出错：`transactional_session` 回滚并向 API 抛出。  
10. 数据库：是。  
11. 长期记忆：经 worker/repository 间接写。  
12. source ID：由下游链路保存。  
13. 示例：`POST /sessions/close` 调用后同事务得到 summary、memory 和 closed 状态。  
14. 限制：默认 fake summarizer/finalizer；固定 `user_memory_enabled=True`；ensure_user 会创建默认 memory disabled 用户，但随后忽略该字段；重复 close 前不检查 active。  
15. 测试：验证完整事务流程、可跳过摘要、API 接线和 SQLite 落库。

## 4.11 `runtime/factory.py:L57-L170` 与 `app/dependencies.py:L40-L98`

1. 最简单的话：决定应用到底使用哪套零件。  
2. 必要性：同一套 API 可以选择内存 demo 或数据库模式。  
3. 重要接口：四个 build 函数、`get_orchestrator`、`get_session_closer`、reset/dispose。  
4. 参数：session factory、LLMClient 或可替换组件。  
5. 返回：TurnOrchestrator、SQL runtime 或 session closer。  
6. 数据来源：环境变量 Settings 和进程缓存。  
7. 结果去向：FastAPI dependency injection。  
8. 正常顺序：读 runtime_mode -> 首次构造 -> 缓存 -> 后续复用。  
9. 出错：不支持的 mode 抛 ValueError；in-memory close 返回 501。  
10. 数据库：SQL 模式构造 engine/session factory。  
11. 长期记忆：closer 路径间接写。  
12. source ID：不处理。  
13. 示例：设置 `RUNTIME_MODE=sqlalchemy` 后 chat 自动摘要，close 可以写记忆。  
14. 限制：SQL runtime 内层反馈/风险/状态/策略/回复/guard 仍多为 fake；模型版 summary/finalizer 没有从 Settings 自动组装；全局缓存要求测试手动 reset。  
15. 测试：API runtime、runtime factory 和依赖对象复用测试。

# 5. 核心数据结构词典

| 数据结构 | 人话含义与主要字段 | 谁创建 / 谁使用 | 是否保存及表 |
|---|---|---|---|
| `Message` | 一条原始消息：id、session、角色、内容、顺序、时间 | MessageRepository 创建；几乎所有 agent/worker 使用 | 是，`messages` |
| `SessionState` | 系统对当前 session 的结构化理解：版本、阶段、目标、话题、情绪、偏好、问题、风险 | StateReducer 创建；planner/context/finalizer 使用 | 是，JSON 存入 `session_state_versions` |
| `InterventionRecord` | 某轮 assistant 用了什么策略，以及下一轮反馈 | InterventionRepository 创建/完成；feedback/summary/finalizer 使用 | 是，`intervention_events` |
| `FeedbackResult` | 用户是否明确接受/拒绝上一策略，以及适配度和进展 | FakeFeedbackEvaluator 创建；repository/reducer/planner 使用 | 嵌入更新 `intervention_events`，没有独立表 |
| `RollingSummarizerInput` | 摘要所需材料包：旧摘要、未覆盖消息、状态、干预 | SummaryWorker 创建；summarizer 使用 | 不单独保存 |
| `RollingSummary` | 当前 session 的版本化短笔记和来源 | summarizer 创建；repo/context/finalizer 使用 | 是，JSON 存 `rolling_summary_versions` |
| `SessionFinalizerInput` | 关闭时的完整材料包 | CloseWorker 创建；finalizer 使用 | 不单独保存 |
| `SessionFinalizerResult` | 关闭总结、未完成话题、行动、候选记忆、策略结果、风险 | finalizer 创建；curator/worker 使用 | 本身没有专表；其中 candidate 可能进入 memory 表 |
| `MemoryCandidate` | “建议记住什么”的候选，不是已保存记忆 | finalizer/curator 创建；policy/repo 使用 | 候选不单独保存；获批后转为 memory 行 |
| `MemoryPolicyInput` | 候选、现有记忆、用户开关和 session 的审批材料 | CloseWorker 创建；MemoryPolicy 使用 | 不保存 |
| `MemoryPolicyDecision` | 是否允许、做什么操作、是否待确认、目标 memory | MemoryPolicy 创建；MemoryRepository 使用 | 不单独保存；影响 memory 状态 |
| `MemoryWriteResult` | repository 实际做没做、做了什么、memory ID | MemoryRepository 创建；worker/API 聚合使用 | 不单独保存 |
| `RollingSummaryRequestedEvent` | “请更新这个 session 摘要”的内部请求 | PostTurnWorker/Closer 创建；SummaryWorker 使用 | 不保存为事件表 |
| `SessionCloseRequestedEvent` | “请结束这个用户的 session”的内部请求 | SqlAlchemySessionCloser 创建；CloseWorker 使用 | 不保存为事件表 |

# 6. 数据库表和 repository

| 表 | 什么时候新增一行 | 谁负责写 | D 的读写关系 |
|---|---|---|---|
| `users` | SQL runtime 首次看到 user ID 时 | `SqlAlchemyUserRepository.ensure_user` | close 前确保存在；当前未读取其 `memory_enabled` |
| `sessions` | SQL runtime 首次处理一个 session 时 | `SqlAlchemySessionRepository.ensure_session` | 摘要更新版本计数；close 修改状态和结束时间 |
| `messages` | 每条 user/assistant 消息保存时 | `SqlAlchemyMessageRepository` | summary/finalizer/curator 读取；不修改旧消息 |
| `session_state_versions` | 每个已处理轮次产生新版状态时 | `SqlAlchemyStateRepository.save_version` | summary/finalizer 读取当前版本 |
| `intervention_events` | 普通 assistant 回复后新增 pending；下一用户轮更新为 evaluated | `SqlAlchemyInterventionRepository` | feedback、summary、finalizer 读取/更新 |
| `rolling_summary_versions` | SummaryWorker 发现新未覆盖消息时 | `SqlAlchemySummaryRepository.save_version` | D 主要写入和读取 |
| `long_term_memories` | policy 允许 CREATE/SUPERSEDE 或待确认写入时；REINFORCE 更新旧行 | `SqlAlchemyMemoryRepository` | D 关闭流程主要写；当前聊天检索不读取它 |

worker 不直接操作 ORM model，是为了让业务流程只面对稳定的 Pydantic 数据合同；这样内存测试和 SQL 数据库可以互换，也避免 agent 获得越权写数据库能力。repository 存在的意义，就是把 SQL、flush、表字段转换和错误处理关在一个“数据柜台”后面。

# 7. Fake、模型版和 fallback

| 组件 | 当前性质 | 是否调用 `StructuredLLMClient` | 出错行为 | 程序强制控制什么 |
|---|---|---|---|---|
| `FakeFeedbackEvaluator` | 关键词规则版 | 否 | 无模型错误 | 反馈枚举和固定结果 |
| `FeedbackEvaluator` | **当前不存在** | 无法调用 | 无法确认 | 无 |
| `FakeRollingSummarizer` | 确定性规则版 | 否 | 直接生成保守摘要 | version/coverage/source 收集 |
| `RollingSummarizer` | 模型版适配器 | 是 | 异常或假 ID -> fake | session_id、summary_version、covered_from/to |
| `FakeSessionFinalizer` | 确定性规则版 | 否 | 直接生成保守结果 | 只从规则支持的明确表达提候选 |
| `SessionFinalizer` | 模型版适配器 | 是 | 异常或任一嵌套假 ID -> fake | session_id；schema 和来源集合 |
| `MemoryCurator` | 确定性规则版，不是模型版 | 否 | 无 fallback | 允许来源、过滤、去重、规范化内容 |
| `MemoryPolicy` | 确定性安全规则 | 否 | 无 fallback | 授权、敏感度、推断禁入、操作类型 |

心理支持项目不能只依赖模型输出，因为模型可能编造消息 ID、把推断写成事实、产生诊断或在服务不可用时失败。当前模型可生成 summary/finalizer 的语义字段和候选列表，但程序本地强制控制摘要覆盖边界、session ID、schema 合法性、来源集合，以及最终能否写长期记忆。

当前应用默认没有把模型版 summarizer/finalizer 组装进 SQL runtime。模型版只在单元测试和 scripted smoke 中得到证明；不能据此说已经接入真实 Qwen。

# 8. source_message_ids 全链路

`source_message_ids` 是“这条总结或记忆依据了哪些原始消息”的证据清单。它让人能够回到 `messages` 表检查系统有没有编造。

完整链路：

```text
Message.id
 -> RollingSummarizerInput / SessionFinalizerInput
 -> RollingSummary / SessionFinalizerResult / MemoryCandidate
 -> MemoryCurator 过滤
 -> MemoryPolicy 检查非空并审批
 -> MemoryRepository
 -> long_term_memories.source_message_ids
```

- RollingSummarizer 在 `agents/rolling_summarizer.py:L262-L281` 建立允许集合；模型给出未知 ID 时，整份摘要 fallback。
- SessionFinalizer 在 `agents/session_finalizer.py:L173-L216` 连顶层、action item、candidate memory、strategy outcome 的嵌套 ID 一起检查；任一伪造 ID 导致整份结果 fallback。
- MemoryCurator 在 `agents/memory_curator.py:L53-L69` 删除 source 为空或不在输入集合中的候选。
- MemoryPolicy 在 `services/memory_policy.py:L78-L109` 至少要求 source 列表非空，但它不再查 messages 表确认 ID。
- MemoryRepository 在 `storage/repositories/memory_repository.py:L151-L161` 把 ID 字符串数组保存到数据库。

具体例子：用户消息 `msg_005` 是“请每次一个小步骤”。finalizer 产生 candidate，source 为 `msg_005`；curator 检查 `msg_005` 确实来自输入消息；policy 判断它是低敏感的明确偏好；repository 把内容和 `["msg_005"]` 写入。若模型写成不存在的 `msg_999`，finalizer 会整体 fallback；即使一个外部调用绕过 finalizer，curator 也会过滤未知来源。

边界：当前来源校验主要证明“ID 在输入里”，不能证明模型生成的每一句内容真的被该 ID 的原文支持。那需要更强的忠实度评测或人工审计。

# 9. 当前已经实现了什么

## 9.1 已实现并接入应用流程

- SQLAlchemy 普通聊天路径已接入 inline post-turn summary。
- SQLAlchemy session close 已接入 runtime 和 `POST /sessions/close`。
- 关闭流程会经过 finalizer、curator、policy、conflict resolver、memory repository，再关闭 session。
- API 可通过 `RUNTIME_MODE` 选择 `in_memory` 或 `sqlalchemy` 聊天 runtime。
- SQL 模式 summary、memory 和 close 有 SQLite integration/smoke 证据。

## 9.2 已实现但只在测试或 smoke 中使用

- `RollingSummarizer` 模型版及其来源校验。
- `SessionFinalizer` 模型版及其嵌套来源校验。
- 两者使用的都是 stub/scripted client 证据，不是真实 Qwen endpoint。
- `MemoryPolicy` 的部分 confirmation、supersede 和 conflict 分支有代码，但用户确认闭环未接 API。

## 9.3 仍然使用 fake

- SQLAlchemy chat runtime 内的 risk、state、feedback、strategy、response、guard 默认都是 fake。
- 应用默认 summary 是 `FakeRollingSummarizer`。
- 应用默认 session close 是 `FakeSessionFinalizer`。
- `FeedbackEvaluator` 模型版不存在。

## 9.4 已有 API 入口

- `POST /chat/turn`
- `POST /sessions/close`（只允许 SQLAlchemy mode）
- 没有 memory 查看、确认、修改、删除 API；`api/routers/memories.py` 为空且未注册。

## 9.5 特别检查结论

| 问题 | 当前代码答案 |
|---|---|
| post-turn summary 是否接入 SQL runtime？ | 是，普通 SQL chat 每轮同步 inline 执行。 |
| session close 是否接入 SQL runtime？ | 是，同事务执行。 |
| API 能否选择 in_memory / sqlalchemy？ | 能；默认 in_memory，环境变量切换。 |
| MemoryRetriever 是否从 DB 取长期记忆？ | 否，永远返回空；当前“能写记忆但不会使用记忆”。 |
| memory_enabled 是否从 users 表读取？ | 否，closer 固定 True。 |
| 是否每轮生成 summary？ | 默认 in-memory 不生成；SQL 普通路径每轮生成/更新；安全提前返回路径不生成。 |
| 是否有真正后台队列？ | 否，只有 Noop 和同步 Inline。 |
| 用户能查看、确认、修改、删除记忆吗？ | 不能，memory router 为空。 |

## 9.6 待确认事项

- 真实 Qwen endpoint 是否能稳定运行 summarizer/finalizer：当前代码和 smoke 无法确认。
- PostgreSQL 而非 SQLite 下的完整运行表现：现有测试主要是 SQLite，无法确认生产表现。
- 谁实际修改了哪些 D 文件：仓库无 commit，无法确认。
- 产品到底希望按轮、按 token、按消息数还是按话题触发摘要：当前配置没有说明，无法确认。

# 10. 当前代码中值得我重点理解的 10 个问题

1. **为什么 agent 不直接写数据库？** 因为 agent 只负责产出候选；worker 控流程，repository 控存储，权限更清楚。  
2. **为什么 candidate memory 不能直接保存？** 因为它可能是模型推断、敏感信息、重复或冲突，必须先过 policy。  
3. **为什么模型不能决定 `covered_to`？** 因为覆盖边界决定哪些原文以后可能不再进入 context，必须由程序根据真实消息顺序强制控制。  
4. **为什么同一个 session 有多个 summary_version？** 为了保留历史、支持审计和增量更新，而不是覆盖旧摘要。  
5. **为什么 InlinePostTurnTaskQueue 不是真后台任务？** 因为 `enqueue` 直接 `await PostTurnWorker`，当前 HTTP 请求要等它完成。  
6. **为什么 session close 放同一事务？** 任一步失败时一起回滚，避免“记忆写了但 session 没关”或反过来。  
7. **为什么 Curator 和 Policy 分开？** Curator 回答“候选是什么”，Policy 回答“是否允许保存”；生成与授权不是同一权限。  
8. **为什么 source ID 错误时整体 fallback？** 因为一个假来源说明模型结果的可信边界已经破坏，局部接受可能漏掉嵌套问题。  
9. **为什么每轮摘要可能不适合真实应用？** 会增加延迟和成本，也让最近原文同时存在于摘要和 recent messages，产生重复。  
10. **为什么能保存 memory 不等于会使用 memory？** 写入路径和检索路径是两条链；当前 Retriever 永远返回空，所以后续回复看不到已写记忆。

# 11. 当前可考虑调整的地方

本节只分析，不修改。

| 检查项 | 当前实际行为 | 可能问题与例子 | 建议方向 | 影响文件 | 风险 | 现在改？ |
|---|---|---|---|---|---|---|
| 每轮摘要 | SQL 普通轮次每轮 inline 摘要 | 三句话产生三个版本，增加延迟/重复 | 按未覆盖消息数或 token 阈值触发 | post_turn worker、factory、config | 中 | 建议近期设计后改 |
| `get_recent(limit)` | Summary/Finalizer 默认只取最新 50 | 100 条都未摘要时，前 50 条可能永远漏掉 | 增加按 sequence 范围读取，先取最老未覆盖段 | message repo、summary worker | 高 | 应优先改 |
| memory 开关 | closer 固定 `True` | users 表为 False 仍可能写 memory | repository 读取用户真实开关并传 policy | closer、user repo/worker | 高（隐私） | 应最优先改 |
| MemoryRetriever | 固定空结果 | 已保存“小步骤”偏好，下次聊天仍不知道 | 注入 MemoryRepository，按 user 读取 active memory | retriever、runtime factory、tests | 中高 | 应优先规划 |
| 两种偏好合并 | “具体下一步”被规范成“每次一个小步骤” | 用户只是本轮想具体，并未表达长期节奏偏好 | 分开 intent 与长期 interaction preference | curator/finalizer/tests | 中 | 建议先补案例再改 |
| state preference 来源 | curator 接受 final_state.user_preferences | 该字段可能来自模型抽取，不一定等于用户明确授权 | 检查 source type/原文语义或只接受明确 user message | curator、state contract/producer | 中高 | 应讨论合同后改 |
| inline queue | `enqueue` 直接 await worker | 摘要模型慢会直接拖慢 chat API | 真正任务队列或提交后执行，并补重试/幂等 | post_turn pipeline、worker_app、runtime | 高 | 功能稳定后改 |
| 重复 close | close 本身幂等，但 finalizer/memory 在 close 前重跑 | 第二次 close 可能 REINFORCE 同一记忆 | 事务开始先锁定并检查 active/closed，closed 直接返回 | closer、session repo、tests | 高 | 应优先改 |
| confirmation 闭环 | policy 可写 pending_confirmation | 没有 API 让用户确认，pending 会一直悬空 | 增加查看/confirm/reject/update/delete 接口和权限检查 | memories router/repo/service | 高 | memory 对用户开放前必须改 |
| dependency cache | engine/runtime/closer 进程级缓存 | 测试或运行时改 env 后仍拿旧对象 | 明确生命周期，测试 fixture 强制 reset，生产禁止热切换 | dependencies/lifecycle/tests | 中 | 当前先文档化 |
| 模型 runtime 接线 | 模型 summarizer/finalizer 只能显式注入 | 配了 LLM env 也仍走 fake | 增加受控 factory 开关、健康检查和真实 endpoint E2E | factory/config/llm/runtime | 中高 | B/C endpoint 稳定后改 |
| 来源语义忠实度 | 只验证 ID 属于输入集合 | 模型可用真实 ID 支撑不相关内容 | 增加 quote/span 或 entailment/evaluation | schemas（需审批）、agents/evaluation | 高 | 先做评测，不急改 schema |

# 12. 我应该按什么顺序阅读代码

1. **先看 `schemas/summary.py` 和 `schemas/memory.py`。** 只看 `RollingSummary`、`SessionFinalizerResult`、`MemoryCandidate`、`MemoryPolicyDecision`。看完应能回答“各环节交接什么表格”；暂时忽略 Pydantic 细节。  
2. **看 `agents/rolling_summarizer.py:L17-L158`。** 对比 Fake 和模型版。看完应能回答“模型失败怎么办、哪些字段程序说了算”；先忽略辅助去重函数。  
3. **看 `workers/summary_worker.py:L20-L89`。** 看完应能回答“数据从哪来、谁保存”；先忽略 repository 的 SQL。  
4. **看 `workers/post_turn_worker.py` 和 `orchestrator/post_turn_pipeline.py`。** 看完应能回答“为什么它不是后台队列”。  
5. **看 `orchestrator/turn_orchestrator.py:L88-L176`。** 重点找反馈和 enqueue 的位置。看完应能回答“D 在一轮主流程的哪里接入”；先忽略 risk/response 细节。  
6. **看 `agents/session_finalizer.py:L51-L216`。** 看完应能回答“关闭总结与来源校验怎么做”；先忽略所有关键词常量。  
7. **看 `agents/memory_curator.py:L40-L157`。** 看完应能回答“候选如何过滤和去重”。  
8. **看 `services/memory_policy.py:L70-L173` 和 `services/conflict_resolver.py:L73-L133`。** 看完应能回答“为什么候选不能直接写”。  
9. **看 `workers/session_close_worker.py:L48-L148`。** 把 finalizer、curator、policy、repository 串起来。  
10. **看 `runtime/sqlalchemy_session_closer.py`、`runtime/factory.py:L124-L170`、`app/dependencies.py:L50-L77`。** 看完应能回答“应用真正用了 fake 还是模型版、API 如何选 runtime”。  
11. **看 summary/memory repositories。** 只看公开方法和 ORM 转换。看完应能回答“worker 为什么不直接写表”；暂时忽略 SQLAlchemy 索引。  
12. **最后看 integration tests 和四个 smoke。** 它们说明哪些能力真的接起来；看到 `ScriptedStructuredModelClient` 时要记住它不是真 Qwen。

## 文档描述与当前代码不一致汇总

1. `docs/data_structure_contracts.md` 说 summary loop 和 memory loop 尚未完整实现；当前代码已经实现并接入 SQLAlchemy/close API，但检索和用户治理仍缺。  
2. `README.md` 只描述最薄 fake 基础和 `/chat/turn`；当前还有 SQLAlchemy runtime、自动摘要和 `/sessions/close`。  
3. 系统设计写“异步 Post-Turn Pipeline”；当前只有同步 inline 和 Noop，不是真后台。  
4. 代码架构设计列出 memory 查看、修改、删除、确认 API；当前 `api/routers/memories.py` 为空且未注册。  
5. 设计目标路径是 `/sessions/{session_id}/close`；当前实际路径是 `POST /sessions/close`，session ID 在 body。  
6. 设计要求按用户授权保存记忆；当前用户表默认 memory disabled，但 close runtime 固定按 enabled 处理。  
7. 设计说下一 session 检索长期记忆；当前 `MemoryRetriever` 永远返回空。  
8. 设计描述 FeedbackEvaluator 模型能力和 prompt；当前只有 Fake，prompt 为空。  
9. 设计推荐 Celery/RQ/Dramatiq、Redis 和 worker app；当前 `worker_app.py` 为空，Redis 未用于 D 流程。  
10. `psych_agent_team_collaboration_plan.md` 与 `docs/detailed_task_breakdown.md` 对 D 的职责归属不同；Git 无法提供作者证据来消除冲突。
