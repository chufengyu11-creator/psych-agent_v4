# 心理支持 Agent v3：完整上下文、已完成改动与后续开发交接

> 更新时间：2026-08-05（Asia/Shanghai）  
> 交接对象：后续接手远程 v3 的开发者 / Codex 账号  
> 本文目标：让接手者不依赖旧聊天记录，也能准确理解项目、当前实现、实测结果、已知问题和下一步方案。

---

## 0. 接手者先看这里

请先遵守以下边界：

1. **只修改远程 v3，不修改 v2。** v2 是速度和行为对照组。
2. 开始修改前先只读检查本文、现有变更记录、进程、数据库和测试状态，并向用户复述你的理解。
3. 用户希望开发期间实时汇报：正在改什么、为什么改、遇到了什么问题、测试结果是什么。
4. 不要把 `.env`、数据库密码、SSH 私钥或其他凭据复制到聊天或文档。
5. 数据库迁移、服务重启、依赖安装、模型重启、删除/覆盖文件前，要先确认影响范围并备份。
6. 当前 v3 的深度状态分析只是 **shadow（影子）模式**：会运行、会计时，但结果不会写库，也不会给下一轮使用。不要误报成已经实现 N+1。
7. v2、v3 目前共用 PostgreSQL；正式增加表或状态字段前，应优先隔离 v3 数据库。

状态标记：

- ✅ 已实现，并做过专项验证
- ⚠️ 已做影子实现或局部实现，尚未形成完整功能
- ❌ 仅讨论过的后续方案，尚未实现

建议先阅读服务器已有记录：

```text
/data/agent/psych-agent/psych-agent_v3/docs/v3_async_state_pipeline_changes.md
```

---

## 1. 项目和运行环境

这是一个面向心理支持对话的多阶段智能体。它不是单纯把用户文本直接发给一个聊天模型，而是在一次回复前后依次处理风险、状态、策略、回复、安全审查、摘要和长期记忆。

### 1.1 目录

本机 Windows 工作区：

```text
D:\psych-agent-v2
```

远程服务器 SSH 别名：

```text
4090_1
```

远程项目：

```text
v2: /data/agent/psych-agent/psych-agent_v2
v3: /data/agent/psych-agent/psych-agent_v3
```

本轮功能代码改动只发生在远程 v3。v2 未改，用作对照。本地工作区此次仅新增本交接文档，没有修改功能代码。

### 1.2 服务和端口

已知运行安排：

```text
Qwen3.6-27B OpenAI-compatible vLLM: 127.0.0.1:8001
Qwen3.5-0.8B-Base OpenAI-compatible vLLM: 127.0.0.1:8002
v2 Web/API: 远程 8000，本机通常通过 SSH 转发到 8010
v3 Web/API: 远程 8011，本机通常通过 SSH 转发到 8011
PostgreSQL: 远程本机 127.0.0.1:5432
```

浏览器地址仅在对应 SSH 端口转发仍然打开时可用：

```text
v2: http://127.0.0.1:8010
v3: http://127.0.0.1:8011
```

重要：v3 `.env` 中的 `APP_PORT` 仍是 `8000`，当前 v3 是通过启动环境/命令覆盖为 `8011`。重启时不能直接照搬 `.env`，否则可能与 v2 冲突。

2026-08-05 的只读运行快照（PID 后续重启会变化）：

```text
v2: tmux psych-agent-web, PID 106792, 127.0.0.1:8000, health=ok
v3: tmux psych-agent-v3, PID 1329687, 127.0.0.1:8011, health=ok
v3 cwd: /data/agent/psych-agent/psych-agent_v3
v3 command: /root/miniconda3/envs/agent/bin/python -B -m uvicorn app.main:app --host 127.0.0.1 --port 8011
v3 tmux: remain-on-exit=on
PostgreSQL: 127.0.0.1:5432
```

模型/GPU 快照：

```text
Qwen3.6-27B: 端口 8001，tensor parallel=4，CUDA 0,1,2,3
Qwen3.5-0.8B-Base: 端口 8002，tensor parallel=2，CUDA 6,7
另有与本项目当前路由无关的 Qwen3.5-9B: 端口 8004，CUDA 4,5
```

因此当前 27B 和 0.8B 服务合计占用 6 张卡；整台机的另一个 9B 服务还占 2 张卡。后续功能设计不必默认增加显卡，但任何后台并发调整都要实测资源争用。

### 1.3 当前模型分工

远程 v3 非敏感配置核对结果：

```text
APP_RUNTIME_MODE=sqlalchemy_model
RISK_MODEL_NAME=Qwen3.5-0.8B-Base
STATE_MODEL_NAME=Qwen3.5-0.8B-Base
FEEDBACK_MODEL_NAME=Qwen3.5-0.8B-Base
STRATEGY_MODEL_NAME=Qwen3.5-0.8B-Base
RESPONSE_MODEL_NAME=Qwen3.6-27B
OUTPUT_GUARD_MODEL_NAME=Qwen3.6-27B
SUMMARY_MODEL_NAME=Qwen3.5-0.8B-Base
SESSION_FINALIZER_MODEL_NAME=Qwen3.5-0.8B-Base
MEMORY_CURATOR_MODEL_NAME=Qwen3.5-0.8B-Base
```

也就是说，主要自然语言回复和当前输出审查走 27B；风险、状态、反馈、策略、摘要、会话总结和记忆提炼主要走 0.8B。模型服务在本次改造前已经存在，本次没有重启或修改它们。

### 1.4 Git 状态风险

2026-08-05 只读检查远程 v3 得到：

```text
## No commits yet on main
```

整个项目当前基本都是 untracked，尚无可用基线提交。因此：

- 不能依赖普通 `git diff` 判断“我改了什么”；
- 不能假设 `git checkout` 可以恢复；
- 不要执行 `git reset --hard`；
- 下一阶段开始前，建议在用户同意后做代码快照，并建立不包含密钥的 Git 基线提交。

本次通过 v2/v3 目录只读比较和手工变更记录核对差异。

---

## 2. 原始前台流程及性能问题

原始核心流程近似为：

```text
用户消息
  -> 上轮反馈评估（第二轮起）
  -> 风险分析 || 模型状态分析
  -> 状态合并和保存
  -> 长期记忆读取
  -> 策略规划
  -> 回复生成
  -> 输出审查
  -> 保存回复并返回
  -> 后台滚动摘要
```

风险和旧状态分析虽然并行，但这一段必须等待较慢者。旧状态分析调用 0.8B 结构化模型，实测使“风险 + 状态”约为 8.90～9.42 秒。后面策略、回复和输出审查又存在真实依赖，因此大量阶段仍然串行。

SQLAlchemy/model 模式的准确入口和事务边界是：

```text
api/routers/chat.py::create_chat_turn
  -> runtime/sqlalchemy_orchestrator.py::SqlAlchemyTurnOrchestrator.handle_turn
  -> 每轮建立一个 transactional_session
  -> 构造绑定该请求 session 的 repositories 和 TurnOrchestrator
  -> 前台成功后提交事务
  -> 提交后再派发 buffered post_turn 任务
```

正常轮的模型依赖实际为：

```text
可选 feedback（第二轮起，串行）
  -> [risk || state]
  -> strategy
  -> response
  -> output_guard
```

前台同一请求中的仓库共用同一个 `AsyncSession`；当前并行的 risk/state 都是模型调用而非并发 DB 操作，所以没有在这个位置并发使用 session。后台摘要会新开独立事务，并按 user/session 锁保持顺序。

用户的优化原则是：

1. 先计时，确认每部分真实耗时；
2. 再判断哪些任务有数据依赖、哪些可挪到后台；
3. 不为了速度盲目减少功能；
4. 当前轮必须立刻影响回复的信息放在快速分析；
5. 更深但允许晚一轮使用的信息放到后台，供 N+1；
6. 用户明确要求“真正会话目标是否变化”和“是否在纠正旧信息”属于快速分析。

---

## 3. v3 已完成的代码改动

### 3.1 v2/v3 实际差异文件

2026-08-05 在生成本交接文件之前，通过 `diff -qr` 核对出的 v3 差异共 17 项如下（不含本交接文件自身）：

```text
agents/state_tracker.py
api/routers/chat.py
app/config.py
llm/local_client.py
llm/structured_client.py
orchestrator/deep_state_pipeline.py              # v3 新增
orchestrator/post_turn_pipeline.py
orchestrator/turn_orchestrator.py
runtime/application.py
runtime/factory.py
runtime/sqlalchemy_orchestrator.py
schemas/state.py
services/state_reducer.py
services/timing.py                               # v3 新增
tests/unit/test_deep_state_pipeline.py           # v3 新增
tests/unit/test_fast_state_tracker.py            # v3 新增
docs/v3_async_state_pipeline_changes.md          # v3 新增
```

接手者修改时要注意：一些文件内部保留了大段注释掉的旧实现，之前曾出现补丁匹配到注释副本的问题。必须确认改的是正在执行的活代码。

### 3.2 计时埋点 ✅

新增：

```text
services/timing.py
```

并接入 HTTP 聊天入口、LLM 客户端、TurnOrchestrator、PostTurnPipeline 等调用点。用途是用同一 trace 区分：

- HTTP 整轮；
- 反馈分析；
- 风险分析；
- 快速/深度状态分析；
- 状态保存；
- 长期记忆检索；
- 策略规划；
- 回复生成；
- 输出审查；
- 后台摘要；
- 会话关闭时的总结和长期记忆提炼；
- 底层结构化 LLM 调用。

`api/routers/chat.py` 会为一次请求建立 trace；`llm/structured_client.py` 会记录 agent、model 和输出 schema；`orchestrator/post_turn_pipeline.py` 会记录后台任务时间。

### 3.3 快速状态分析 ✅

主要文件：

```text
schemas/state.py
agents/state_tracker.py
services/state_reducer.py
orchestrator/turn_orchestrator.py
```

已完成：

- 在 `SessionState` 和 `StateDelta` 中增加 `current_turn_goal`；
- 新增确定性的 `FastStateTracker`，前台不调用大模型；
- 识别明确的当前目标、会话目标变化、显式情绪和直接纠正（例如“不是 A，是 B”）；
- `StateReducer` 采用纠正优先；
- 被纠正的旧值会停用，同时保留证据历史；
- 修复“否定掉的旧情绪又被关键词规则重新加入”的问题。

快速状态自身实测约：

```text
0.15～0.23 ms
```

### 3.4 深度状态后台管线 ⚠️

新增：

```text
orchestrator/deep_state_pipeline.py
```

并修改：

```text
agents/state_tracker.py
orchestrator/turn_orchestrator.py
runtime/sqlalchemy_orchestrator.py
runtime/factory.py
runtime/application.py
```

已完成的基础设施：

- `StateTracker` 提供深度模型分析入口；
- 调度前深拷贝输入，避免共享可变对象；
- 同一 session 使用锁保持任务顺序；
- 全局深度推理并发限制为 1，降低 0.8B 资源竞争；
- 不把前台事务的 SQLAlchemy `AsyncSession` 传给后台；
- 后台异常会被回调消费、计数和记录，不让异常无人处理；
- 应用关闭会排空自己拥有的后台任务；
- 策略规划后启动深度分析，使它可与回复生成重叠；
- 同一 session 的滚动摘要会等待深度任务结束。

**尚未完成、必须特别强调：**

- 深度结果没有写入任何表；
- 没有合并进正式 `SessionState`；
- N+1 不会读取它；
- 当前回调只记录耗时和结果字段数量，随后结果被丢弃；
- 摘要虽然等待深度任务，但当前并不消费深度结果。

所以当前只是“异步方式和安全边界的性能验证版”。

### 3.5 当前 v3 编排顺序

功能开关打开时：

```text
前台：
feedback
  -> (risk || fast state)
  -> reduce/save state
  -> memory retrieve
  -> strategy
  -> enqueue deep shadow
  -> response
  -> output guard
  -> commit/return

后台：
deep shadow（同 session 顺序执行，全局并发 1）
  -> post-turn rolling summary
```

当前运行时需要：

```text
ASYNC_STATE_PIPELINE_ENABLED=true
```

但 `app/config.py` 中默认仍为 `false`。漏掉环境变量会退回旧的模型型前台状态分析。

只读检查确认当前 v3 进程环境里确实有：

```text
ASYNC_STATE_PIPELINE_ENABLED=true
PYTHONDONTWRITEBYTECODE=1
```

但 `.env.example` 仍与 v2 相同，没有记录新开关。下一位若完善启动配置，必须只改 v3，并避免把真实凭据提交进去。

### 3.6 测试结果

新增专项测试：

```text
tests/unit/test_fast_state_tracker.py
tests/unit/test_deep_state_pipeline.py
```

已验证：

```text
6 个专项测试通过
19 个相邻回归测试通过
静态补丁检查、Python 编译/导入检查通过
```

全量测试当时结果：

```text
519 passed
21 failed
65 errors
```

失败/错误主要进入了测试数据库环境，包括缺少 `aiosqlite` 和配置的 PostgreSQL 测试迁移不可用或失败。没有为了“让数字好看”而私自安装依赖或手动迁移测试库。

注意：这不能等价于“全部失败都已证明与本功能无关”。下一位需要先固定测试环境，再重新跑全量套件。

### 3.7 修改过程中遇到的问题

这些信息有助于避免重复踩坑：

- 曾发现并修复缺失的 `MessageId` import；
- 否定情绪被规则重新加入，已加排除逻辑和回归测试；
- 一次补丁匹配到注释旧副本，应用前被发现并重新生成；
- `ApplicationRuntime` 曾出现缩进错误，导入检查发现后已修复；
- 一次 v3 tmux 会话消失，最初没有 `remain-on-exit`，无法恢复准确退出原因；后来重建并打开 `remain-on-exit`；
- SSH 连接曾多次重置，所以每次写入后都做了核验；
- 一个测试文件曾被非 UTF-8 传输方式损坏，确认是新文件后重建并重新测试。

---

## 4. 性能实测记录

以下数据来自不同测试轮次和不同输入，不应把单条数值当作严格 benchmark；同组平均值可用于判断趋势。

### 4.1 改造前基线

```text
风险 + 旧模型状态分析：约 8.90～9.42 s
完整第一轮：约 17.53 s
完整第二轮：约 25.65 s
```

### 4.2 影子异步改造后的专项轮次

第一轮：

```text
客户端：15.95 s
服务端整轮：15.79 s
风险 + 快速状态：0.832 s
快速状态：0.202 ms
策略：9.43 s（触发现有截断回退）
深度影子：0.984 s
回复：3.24 s
输出审查：2.10 s
```

第二轮（含显式纠正）：

```text
客户端：11.17 s
服务端整轮：10.70 s
反馈：1.44 s
风险 + 快速状态：0.735 s
快速状态：0.229 ms
策略：1.90 s
深度影子：8.91 s（0.8B 结构化输出截断）
回复：4.04 s
输出审查：2.55 s
后台摘要：6.62 s（结构化输出也失败，但未影响前台回复）
```

第二轮状态落库检查：旧情绪 `pressure` 已 inactive，新纠正情绪 `angry` 已 active，当前目标和会话目标反映用户的新请求。

### 4.3 v2/v3 四轮对比（长期记忆关闭）

```text
v2: 16.20, 18.86, 19.79, 20.51 s；平均 18.84 s
v3:  7.68, 16.46, 10.51, 10.84 s；平均 11.37 s
```

该组 v3 平均缩短约 39.6%。

### 4.4 开启长期记忆的预热对比

```text
v2: 23.75, 22.34, 25.82, 18.45 s；平均 22.59 s
v3: 15.13,  8.65,  9.56, 10.22 s；平均 10.89 s
```

### 4.5 新会话读取已有长期记忆

```text
v2 完整轮：17.21 s
v3 完整轮： 8.06 s
v3 memory.retrieve 数据库阶段：约 1.214 ms
```

结论：每轮长期记忆数据库读取不是主要瓶颈；主要耗时仍是模型调用和串行依赖。

### 4.6 关闭会话、生成长期记忆

一次实测：

```text
滚动摘要：3.29 s
会话最终总结：7.67 s
长期记忆提炼：6.57 s
合计约：17.53 s
```

数据库写入本身不是主要耗时，主要是三个模型阶段。它发生在关闭会话流程，不是每次读取记忆都要付出的时间。

---

## 5. 长期记忆：存储、读取和当前缺陷

### 5.1 存储位置与表

PostgreSQL 主要表：

```text
users                       # 用户和 memory_enabled
sessions                    # 会话
messages                    # 对话消息
session_state_versions      # 版本化会话状态 JSON
rolling_summary_versions    # 滚动摘要
long_term_memories          # 长期记忆
intervention_events         # 策略/干预跟踪
```

长期记忆不是服务器目录里的普通文件。

当前流程：

```text
网页开启长期记忆（只是授权写入）
  -> 进行对话
  -> 点击关闭会话
  -> 滚动摘要
  -> SessionFinalizer
  -> MemoryCurator
  -> MemoryPolicy
  -> 写 long_term_memories
```

准确调用链是：

```text
api/routers/sessions.py::close_session
  -> runtime/sqlalchemy_session_closer.py::SqlAlchemySessionCloser.close_session
  -> 可选 SummaryWorker / RollingSummarizer
  -> workers/session_close_worker.py::SessionCloseMemoryWorker
  -> SessionFinalizer（模型）
  -> MemoryCurator（模型）
  -> 对候选逐条执行 MemoryPolicy 和 repository 写入
  -> 关闭 session 并提交事务
```

其中 summary、finalizer、curator 主要是串行模型阶段，因此关闭会话较慢；逐条数据库写入不是主要耗时。

当前并非每说一句话就实时产生正式长期记忆。

### 5.2 记忆状态

常见值：

```text
active
pending_confirmation
superseded
conflicted
expired
deleted
```

`RepositoryMemoryRetriever` 当前只通过 `list_active(user_id)` 读取 active 记录，按更新时间倒序，最多取约 8 条。其 `query` 和 `session_state` 参数当前实际上被忽略，没有语义相关性搜索。

另外，`RetrievedMemories` 当前把 `interaction_preference` 和 `active_goal` 降成纯字符串，而 semantic/episodic 才保留完整 `LongTermMemory` 对象。因此前两类会丢失 memory ID，不利于可靠引用和审计。`ResponseAgent` 目前只会保留它认识的 memory ID，`OutputGuard` 的记忆检查也主要防止未知 ID，并不检查“数据库有 active 记忆但回复声称没有”。

`pending_confirmation` 不会进入正常回复上下文。当前网页又没有确认入口，因此待确认记录会留在库里但不能被使用。

### 5.3 当前 Web/API 能力缺口

现有网页/API 主要支持：

- 查看/切换 `memory_enabled`；
- 关闭会话，并显示候选数和写入数。

尚无：

- 记忆列表；
- active/pending 状态查看；
- 确认待确认记忆；
- 删除、编辑记忆；
- 查看某条回复引用了哪些 `memory_id`。

代码中虽存在 `api/routers/memories.py`，当前是空文件，`app/main.py` 也没有挂载可用的 memories router，因此不能把“文件存在”误解成已经有记忆管理 API。

### 5.4 “v2 记得音乐节，v3 却说不记得”的调查结论

测试使用过 `v3-memory-on` 用户。数据库里存在 active 记忆，也存在关于具体歌手/音乐节的 pending 记忆。

调查确认：

- v2、v3 使用同一数据库；
- 两边主要回复模型相同；
- v3 实际已经取到了 active 记忆；
- 精确的华晨宇/音乐节内容当时处于 `pending_confirmation`，因此不会被正常取出；
- 可取出的 active 记录较模糊，例如音乐节、抢票、互动或交流偏好；
- v2 偶然在回复中使用了模糊 active 记忆；
- v3 忽略了已传入的 active 记忆，并错误声称没有记录。

根因不是数据库读取慢，而是：

1. 没有确定性识别“你记得我吗 / 我以前说过什么 / 长期记忆里有什么”；
2. `StrategyPlannerInput` 目前只有 state、risk、feedback、memories，没有当前原始用户文本；
3. 这些询问未必让 FastStateTracker 产生状态变化，策略就继续走普通情绪探索；
4. 回复模型虽然获得 memories，但没有必须引用它的硬约束；
5. 有 active 记录时，没有禁止模型声称“完全没有记录”；
6. `DraftResponse` 已有 `referenced_memory_ids` 字段，但尚未形成可靠的存在性、归属和实际引用校验；
7. pending 没有确认 UI；
8. 当前部分记忆是推测性交流偏好，不是清晰、稳定、可复述事实；
9. 深度状态影子结果没有提供给下一轮。

### 5.5 查看数据库（只读）

在远程终端可直接连接：

```bash
psql -h 127.0.0.1 -U psych_agent -d psych_agent
```

如果服务器没有 `psql` 客户端，可在项目目录使用 PostgreSQL 容器：

```bash
docker compose -f compose.postgres.yml exec postgres psql -U psych_agent -d psych_agent
```

常用只读查询：

```sql
\pset pager off
\x on

SELECT *
FROM long_term_memories
WHERE user_id = '实际用户ID'
ORDER BY updated_at DESC;

SELECT id, memory_type, status, confidence, content, updated_at
FROM long_term_memories
WHERE user_id = '实际用户ID'
  AND status = 'active'
ORDER BY updated_at DESC;

\q
```

不要把示例中的 `'实际用户ID'` 或 `'userid'` 当作固定值；它必须替换成网页左侧真实填写的用户 ID。

---

## 6. v2/v3 共用数据库的兼容问题

状态：❌ 尚未正式解决。

v3 把新增的 `current_turn_goal` 写入 `session_state_versions` 的状态 JSON。v2 的旧 `SessionState` 严格禁止额外字段。v2 读取由 v3 写过的同一用户/同一 session 状态时，会出现 Pydantic `ValidationError`，网页表现为 `Internal Server Error`。

当前临时规避：

- v2 与 v3 使用不同 user ID；
- v2 与 v3 使用不同 session ID。

测试用过的隔离 ID：

```text
v2-test / v2-session-1
v3-test / v3-session-1
v2-memory-on / v2-memory-1...
v3-memory-on / v3-memory-1...
```

这只是绕开，不是修复。后续正式做数据库迁移前，应优先让 v3 使用独立数据库、独立 schema 或独立数据库名；必要时只复制用于测试的长期记忆。不要为了兼容而随意改 v2，因为 v2 是对照基线。

---

## 7. 已讨论的快速分析 / 深度分析分工

### 7.1 快速状态分析

原则：当前轮必须立即影响策略或回复、且能高置信度确定的信息。目标是不新增模型调用，尽量控制在 1～5 ms。

应包括：

1. 直接安全风险信号（风险仍由 RiskAgent 负责，快速层可提供确定性信号）；
2. 用户明确表达的当前目标；
3. 用户真正会话目标是否明确改变；
4. 用户是否在纠正之前的信息；
5. 明确的情绪表达；
6. 明确的否定、同意、拒绝；
7. 当前消息是否为长期记忆查询；
8. 当前轮必须立即生效的显式事实。

其中 2～5 已局部实现；“记忆查询意图”尚未实现。

### 7.2 深度状态分析

原则：允许晚一轮使用、需要多轮上下文或带推断性质的信息。不得阻塞当前回复。

应包括：

1. 隐含情绪和情绪趋势；
2. 较稳定的交流偏好；
3. 多轮关系和信任变化；
4. 潜在但未明确表达的目标；
5. 未解决话题；
6. 长期行为/表达模式；
7. 可供后续策略使用的深层状态；
8. 值得提炼、但不应覆盖显式事实的推断。

当前模型任务会运行，但结果完全没有接入正式状态。

---

## 8. 下一阶段建议：先修长期记忆可靠性

状态：❌ 尚未实现。预计难度 4～5/10，收益高、风险相对低，不需要增加模型调用。

建议先做：

1. 增加确定性记忆查询意图识别，例如 `memory_query`；
2. 安全路由优先：风险不为 normal 时仍先走安全流程；
3. 对正常记忆询问，可以生成确定性策略计划，避免让 0.8B 把它误判为普通情绪探索；
4. 将当前原始用户文本或明确的 turn intent 传入策略输入；
5. active 记忆存在时，回复不得声称“完全没有任何记忆”；
6. 使用 `DraftResponse.referenced_memory_ids`，验证 ID 真实存在并属于当前用户；
7. 不要用额外一次模型重试纠错；若模型违规，使用确定性模板回退，避免增加耗时；
8. 没有 active 时要区分：确实无记录、只有 pending、用户 ID 不一致；
9. 记忆查询本身可以跳过普通策略模型，反而可能更快；
10. 给这条路径增加专项日志和测试，但不要记录完整敏感用户文本。

更可靠的首版做法是：针对“是否记得 / 列出记忆 / 回忆某件事”走专用确定性分支，构造一个确定性 strategy/context 和 `DraftResponse`；有 active 就只列真实记录，无 active 才能说没有“可用的已确认记忆”。该分支可以跳过普通 strategy 和 response 两次模型调用，但仍保留风险优先和必要的输出安全处理。若暂时不做专用分支，至少要把当前原始消息或 typed intent 加到 `StrategyPlannerInput`，但可靠性仍低于确定性回答。

为了让所有记忆类型都可审计，可以给 `RetrievedMemories` 增加完整对象集合，或在保留旧字符串字段兼容现有策略逻辑的同时，让 preference/goal 也携带 `id`。不要只靠回复文本猜测引用了哪条记忆。

可能涉及的文件（以实际设计为准）：

```text
schemas/strategy.py
schemas/context.py
schemas/safety.py                    # 已有 referenced_memory_ids，可增强校验
services/memory_retriever.py
services/context_builder.py
services/（新增 memory intent/response validation 服务）
orchestrator/turn_orchestrator.py
agents/strategy_planner.py
agents/response_agent.py
agents/output_guard.py
storage/repositories/memory_repository.py
api/routers/memories.py
app/main.py
tests/unit/
tests/integration/
```

验收必须覆盖：

- 有 active 时问“你记得我吗”，必须引用真实 active 内容；
- 有 active 时不能说“没有任何记录”；
- 只有 pending 时明确说有待确认记录，而不是把它当 active；
- 无任何记录时诚实回答；
- 不同用户不能串记忆；
- 引用的 memory ID 必须属于该用户；
- 不新增模型调用，前台速度不能明显退化。

---

## 9. 下一阶段建议：增加记忆管理 API / 网页

状态：❌ 尚未实现。预计难度 5～6/10。

建议提供：

- 查看记忆列表；
- 按 active / pending / expired 等状态筛选；
- 确认 pending；
- 删除；
- 必要时编辑；
- 显示类型、来源、置信度和更新时间；
- 明确敏感信息和授权边界。

可能涉及：

```text
storage/repositories/memory_repository.py
api/routers/users.py 或新增 memories router
API response/request schemas
web/index.html
web/app.js
web/styles.css
repository/API/web tests
```

确认与删除必须验证 `user_id` 所有权；不能通过任意 memory ID 操作其他用户数据。最好使用软删除语义并保留审计信息。

---

## 10. 下一阶段建议：N / N+1 深度状态正式接入

状态：❌ 尚未实现。预计难度 7～8/10，是后续最难部分。

### 10.1 建议流程

```text
N 轮消息到达
  -> 读取已提交正式状态
  -> 读取 N-1（或更早）已 ready 且未消费的深度候选
       -> 有：按版本和字段优先级合并
       -> 没有：绝不等待，继续
  -> 执行 N 轮快速分析
  -> 当前轮明确纠正/目标最后覆盖旧推断
  -> 策略规划、回复、输出审查、返回
  -> 后台执行 N 轮深度分析
  -> 使用独立 DB session 写“候选结果”
  -> 供第一个后续符合条件的轮次消费
```

严格优先级：

```text
当前轮明确纠正
  > 当前轮明确目标和显式事实
  > 上一轮深度推断
  > 更旧历史状态
```

核心原则：后台任务**不能直接异步改正式 SessionState**。它只能写候选；正式合并只能在后续前台事务中进行。

安全的第一版建议只消费“紧邻上一条用户消息且已经 completed”的结果。如果 N 的深度结果在 N+1 开始时仍未完成，N+1 不等待，并将该迟到结果在之后标记为 `superseded/expired`；不要到 N+2 再无条件应用。未来若确实要接受更晚结果，只允许合并 additive、无冲突字段，并严格比较 source sequence。

### 10.2 建议结果表

可以新增类似：

```text
pending_deep_state_results
```

建议至少包含：

```text
id
user_id
session_id
source_message_id
source_turn_number
base_state_version
pipeline_version
result_payload JSONB
status                 # pending / ready / applied / expired / failed
created_at
finished_at
applied_at
error_category
```

唯一约束建议覆盖：

```text
(user_id, session_id, source_message_id, pipeline_version)
```

避免重试产生重复候选。

### 10.3 必须解决的六类风险

#### A. N 结果晚到覆盖 N+1

- 候选记录 `base_state_version`；
- 正式状态单调递增版本；
- 使用 CAS/乐观锁；
- N+1 开始时没 ready 就跳过，不等待；
- 晚到结果只允许字段级无冲突合并，或标记 expired；
- 永远不能覆盖当前轮明确纠正/目标。

#### B. 同一个 SQLAlchemy AsyncSession 并发使用

- 绝不把前台 `AsyncSession` 或 ORM 实例交给后台；
- 后台只接收不可变 DTO / ID；
- 后台通过 `async_sessionmaker` 打开独立短事务；
- 每个任务独立 commit/rollback/close；
- 进程内锁之外，还要依靠 DB 唯一约束和 CAS 支持多进程。

还要处理“queued job 所在事务尚未提交，后台已经完成推理”的可见性问题。不要假设后台事务能读到前台未提交的行。可选的安全实现是：

1. 最简单首版：前台事务先提交 queued job，提交后再派发深度推理；代价是不能与当前回复完全重叠；或
2. 推理可先在内存中进行，但数据库写入必须等待一个明确的 post-commit 信号/可靠 outbox；父事务回滚时取消或丢弃结果。

两种方案需要通过故障注入验证，不能只依靠进程内 `asyncio.Lock`。

#### C. 深度未完成，摘要先生成

当前已有“摘要等待深度任务”的顺序，但摘要还不使用结果。正式版建议：

```text
深度完成 -> 持久化候选 -> 摘要读取已验证候选 -> 生成摘要
```

整条链路都在后台，不能让用户回复等待。

#### D. 后台 0.8B 拖慢前台

- 当前全局并发 1 先保留；
- 测开启/关闭后台时前台 p50/p95；
- 给后台设队列上限、超时和丢弃/降级策略；
- 前台优先；
- 必要时固定到独立 GPU，或把深度启动推迟到前台回复返回后；
- 先测再决定，不必先增加显卡。

#### E. 用户纠正被旧推断覆盖

- 字段级记录来源、来源轮次和显式/推断类型；
- 当前显式事实最高优先；
- 深度结果不得直接激活被用户纠正为错误的旧值；
- 增加专门的乱序测试。

#### F. 深度失败产生无人处理异常

- 当前 done callback 已消费异常；
- 正式版还要写 `failed` 状态和简短错误类别；
- 当前回复继续成功；
- 重试必须幂等并有上限；
- 应用关闭时排空或安全取消任务。

### 10.4 可能涉及文件

```text
schemas/state.py
新建 schemas/deep_state.py（或等价 contract）
storage/models/
storage/models/registry.py
storage/repositories/
storage/repositories/state_repository.py          # CAS / expected version
storage/migrations/versions/
orchestrator/deep_state_pipeline.py
orchestrator/turn_orchestrator.py
orchestrator/post_turn_pipeline.py
runtime/sqlalchemy_orchestrator.py
runtime/factory.py
runtime/application.py
services/state_reducer.py
tests/unit/
tests/integration/
tests/postgres/
```

---

## 11. 推荐实施顺序

### 第 0 步：保护和隔离

- 只读核对进程、端口、环境变量和健康状态；
- 备份远程 v3 和 PostgreSQL；
- 建立不包含 `.env` 的 Git 基线；
- 将 v3 数据库/schema 与 v2 隔离；
- 重跑 6 个专项测试、19 个相邻测试和当前计时基线。

### 第 1 步：记忆查询可靠性

- 确定性 intent；
- 策略/回复硬约束；
- `referenced_memory_ids` 校验；
- 无额外 LLM 重试；
- 完整专项测试。

### 第 2 步：记忆管理 API/UI

- 列表、确认、删除；
- 所有权校验；
- pending 形成可闭环功能。

### 第 3 步：只持久化深度候选

- 新表和迁移；
- 后台独立 session；
- 幂等键；
- 只写 pending/ready/failed，不改正式状态。

### 第 4 步：N+1 消费和版本合并

- 前台读取 ready；
- 当前 fast 最后覆盖；
- CAS；
- applied 与状态保存处于安全事务边界；
- 迟到/重复/乱序测试。

### 第 5 步：摘要和长期记忆质量

- 摘要真正消费已验证深度结果；
- 区分事实、事件、偏好和推断；
- 降低模糊、推测性长期记忆；
- 改善相关性检索。

### 第 6 步：性能和资源调度

- 查策略阶段的高波动和截断回退；
- 检查意外重复请求；
- 测后台开关前后的 p50/p95；
- 评估提示词和结构化输出预算；
- 不以明显降低心理支持质量来换取少量速度。

整体正式改造粗略难度约 8/10，估计 3～6 个工作日，取决于分库、迁移、UI 范围和测试环境。现有显卡不一定要增加；先验证后台任务是否争抢前台 GPU。

---

## 12. 验收测试清单

### 12.1 快速状态

- 明确改变目标，当前轮立即生效；
- 纠正人物、事件、情绪或偏好，旧值不再激活；
- 没有改变时不误判；
- 不产生模型调用；
- 稳定低于 5 ms。

### 12.2 长期记忆

- active 存在时，“你记得我吗”必须引用真实内容；
- active 存在时不得说“没有任何记忆”；
- 只有 pending 时明确说明等待确认；
- 确认后下一会话可读取；
- 删除后不再读取；
- 不同用户不能串记忆；
- 引用 ID 必须真实且属于当前用户；
- 用户关闭长期记忆时不写入新长期记忆；
- 修复不得新增模型调用。

### 12.3 N+1

- N 深度结果在 N+1 前 ready，只应用一次；
- 故意延迟 N 结果，N+1 不等待；
- 迟到 N 不覆盖 N+1 纠正；
- N+1 改目标后不能被旧推断改回；
- 同一消息重试不重复生成/应用；
- 状态版本单调递增；
- 不兼容结果跳过或只做无冲突合并。

### 12.4 SQLAlchemy / 并发

- 后台不复用前台 AsyncSession；
- 两个不同 session 可并发；
- 同一 session 保序；
- 多进程依靠唯一约束和 CAS 仍正确；
- 不出现 concurrent operation 类错误；
- 数据库失败不影响已完成的当前回复；
- 应用关闭时任务排空或安全取消。

### 12.5 故障注入

- 深度模型超时；
- 非法结构化输出；
- DB 短暂失败；
- 任务取消；
- 服务重启；
- 重复请求。

均应满足：有结构化日志、不泄露用户正文、没有未处理异常、不覆盖正式状态、不阻塞当前回复。

### 12.6 性能

用相同脚本比较：

```text
v2
v3 feature off
v3 feature on
v3 memory off/on
v3 deep background off/on
```

建议目标：

- v3 前台平均耗时不比当前明显退化；
- deep 开启后 27B 前台 p95 增幅最好小于 10%；
- 记忆 DB 检索保持毫秒级，至少低于 50 ms；
- 记忆查询修复不新增模型调用；
- 同时人工检查内容质量，不能只看速度。

---

## 13. 用户偏好与协作方式

接手者应知道：

- 用户对代码和终端操作仍在熟悉阶段，命令需要明确说明“在哪个窗口输入、看到什么算成功、如何退出”；
- 用户希望先听原理和影响，再授权较大的改动；
- 用户明确区分“只分析”和“开始修改”，未授权时不要改；
- 修改时必须实时汇报；
- 保护 v2，不要让实验污染基线；
- 先量化耗时，再优化；
- 用户重视回复质量，不接受为了速度简单砍掉关键功能；
- 可以直接操作远程 v3，但服务重启、数据库迁移和破坏性操作要先说明；
- 用户可能更换 ChatGPT/Codex Plus 账号。SSH 配置、密钥、远程文件、模型服务、数据库和 VS Code Remote SSH 都属于电脑/服务器环境，不会因为换账号而消失；但旧聊天上下文不会自动转移，因此应以本文为交接依据。

---

## 14. 下一位接手者建议的第一轮动作

先不要直接编码。建议按此顺序只读核对并向用户汇报：

1. 阅读本文和 `docs/v3_async_state_pipeline_changes.md`；
2. `git status --short --branch`，确认仍没有可靠 Git 基线；
3. 比较 v2/v3 差异，确认本文文件清单；
4. 查看 v2/v3 健康状态、端口和 tmux；
5. 确认 v3 进程的 `ASYNC_STATE_PIPELINE_ENABLED=true` 与端口 8011；
6. 只读查询 `long_term_memories` 和当前测试用户状态；
7. 重跑现有专项测试；
8. 向用户提出“先分库/备份，再修记忆查询可靠性”的实施计划；
9. 获得授权后再改代码；
10. 每完成一个小阶段就跑测试和报告耗时，不要一次性大改后才检查。

---

## 15. 一句话结论

当前 v3 已成功把昂贵的模型状态分析移出前台，并用毫秒级快速状态保证显式目标和纠正及时生效，平均回复速度明显改善；但深度结果仍被丢弃，长期记忆虽然能从数据库取到，却缺少记忆查询意图、强制引用、确认 UI 和可靠校验。下一步应先隔离 v3 数据库并修复记忆查询可靠性，再实现深度候选持久化与带版本约束的 N+1 消费。
