# 心理支持对话 Agent：系统设计说明书

> 本文描述系统层面的目标、模块边界、Subagent 职责、状态与记忆设计，以及各模块之间的生产—消费链路。  
> 第一版定位为“心理支持与情绪疏导助手”，不承担诊断、治疗、药物建议或替代专业人员的职责。

---

## 1. 系统目标

系统需要解决的核心问题不是“生成一段听起来温柔的回答”，而是：

1. 在单次长对话中持续理解当前主题、用户目标与对话进展；
2. 在跨会话场景中保留经过授权、仍然有效的信息；
3. 根据用户对上一轮回复的反应，动态调整下一轮对话策略；
4. 保持状态、摘要、长期记忆与原始消息之间的可追溯性；
5. 在风险场景中切换到受控的安全流程；
6. 允许未来替换模型、加入微调模型或多模态模块，而不推倒状态系统。

---

## 2. 核心设计原则

### 2.1 LLM 负责理解与候选生成，程序负责控制

LLM 可以：

- 提取用户当前意图；
- 生成状态增量；
- 建议对话策略；
- 生成候选回复；
- 生成候选摘要；
- 提出候选长期记忆；
- 评估用户对上一轮策略的显式反应。

LLM 不可以直接：

- 覆盖数据库中的真实状态；
- 永久写入敏感长期记忆；
- 删除用户数据；
- 跳过安全流程；
- 把模型推测写成用户事实；
- 在线更新模型参数。

### 2.2 State 与 Context 分离

- **State**：数据库中保存的、可版本化、可追溯的系统状态。
- **Context**：某一轮临时拼给模型的输入。

数据库保留完整状态；Context 只包含当前轮最相关的信息。

### 2.3 原始消息是最终事实来源

摘要、记忆与状态都是派生数据。任何重要结论都应该能够追溯到原始消息。

### 2.4 当前信息优先于历史信息

冲突处理优先级：

1. 当前用户明确陈述；
2. 最近用户明确陈述；
3. 用户确认过的长期状态；
4. 历史摘要；
5. 模型推测。

### 2.5 不进行实时在线微调

用户反馈只用于：

- 更新当前会话状态；
- 切换对话策略；
- 更新策略效果记录；
- 生成离线评测样本。

模型参数更新必须经过离线审核、训练、测试和版本发布。

---

## 3. 总体架构

```text
用户消息
   ↓
Turn Orchestrator
   ├── Feedback Evaluator
   ├── Risk Agent
   ├── State Tracker
   ├── State Reducer
   ├── Memory Retriever
   ├── Context Builder
   ├── Strategy Planner
   ├── Response Agent
   ├── Output Guard
   └── Post-Turn Pipeline
          ├── Rolling Summarizer
          ├── Memory Curator
          ├── State Conflict Resolver
          └── Audit Logger
   ↓
最终回复
```

Orchestrator 是普通程序，不是一个自由决策的大模型。它负责调用顺序、权限、路由、异常处理和数据落库。

---

## 4. 核心模块与 Subagent

## 4.1 Turn Orchestrator

### 目的

控制每一轮用户消息的完整生命周期。

### 输入

- `user_id`
- `session_id`
- 当前用户消息
- 当前会话元数据

### 主要职责

1. 保存用户消息；
2. 查找是否存在待评估的上一轮 intervention；
3. 调用 Feedback Evaluator；
4. 并行或串行调用 Risk Agent 和 State Tracker；
5. 根据 Risk Agent 结果选择正常路径或安全路径；
6. 调用 State Reducer 更新 Session State；
7. 调用 Memory Retriever；
8. 调用 Strategy Planner；
9. 调用 Context Builder；
10. 调用 Response Agent；
11. 调用 Output Guard；
12. 保存回复并创建新的 pending intervention；
13. 触发异步摘要和记忆更新任务。

### 生产内容

- 当前轮处理结果；
- 最终回复；
- 新的 intervention 记录；
- 后台任务事件。

### 消费者

- API 层；
- 后台任务系统；
- 日志和评测系统。

---

## 4.2 Risk Agent

### 目的

判断当前消息是否应进入普通对话、进一步安全澄清、危机流程或真人转接。

### 输入

```json
{
  "current_message": "...",
  "recent_messages": [],
  "current_risk_state": {}
}
```

### 输出

```json
{
  "risk_level": "low",
  "categories": [],
  "needs_clarification": false,
  "route": "normal_dialogue",
  "reason_codes": [],
  "confidence": 0.92
}
```

### 允许的 route

- `normal_dialogue`
- `safety_clarification`
- `crisis_protocol`
- `human_escalation`

### 生产—消费关系

- **生产者**：用户消息、当前风险状态；
- **消费者**：Safety Router、Session State、Output Guard、Audit Logger。

### 约束

Risk Agent 只做风险识别与路由建议，不负责自由生成危机回复。

---

## 4.3 State Tracker

### 目的

从当前用户消息中提取相对于上一轮状态的增量变化。

### 输入

- 当前用户消息；
- 上一版 Session State；
- 最近几轮消息；
- 上一轮 feedback。

### 输出

```json
{
  "explicit_user_request": "希望获得具体行动建议",
  "topic_updates": [
    {
      "operation": "add",
      "topic": "与直属领导沟通"
    }
  ],
  "goal_updates": [],
  "reported_emotions": [
    {
      "label": "焦虑",
      "source_message_id": "msg_102"
    }
  ],
  "user_corrections": [],
  "strategy_preferences": [
    {
      "operation": "add",
      "value": "当前不希望继续情绪探索"
    }
  ],
  "hypotheses": []
}
```

### 生产—消费关系

- **生产者**：用户消息、历史 Session State；
- **消费者**：State Reducer、Strategy Planner、Audit Logger。

### 约束

State Tracker 输出的是 `delta`，不得直接替换完整状态。

---

## 4.4 State Reducer

### 类型

确定性程序模块，不是 Subagent。

### 目的

将 State Tracker 产生的增量合法地合并进当前 Session State。

### 主要职责

- 校验字段；
- 应用 `add/update/remove/resolve_conflict` 操作；
- 写入版本号；
- 保留旧版本；
- 拒绝非法更新；
- 将“用户明确陈述”和“模型推测”分开保存。

### 输入

- 上一版 Session State；
- `state_delta`；
- 风险结果；
- feedback 结果。

### 输出

- 新版 Session State；
- 状态变更事件。

---

## 4.5 Feedback Evaluator

### 目的

判断用户当前消息对上一轮对话策略产生了什么可观察反馈。

### 核心原则

用户第 `t` 轮输入，同时是对 Assistant 第 `t-1` 轮 intervention 的反馈。

### 输入

```json
{
  "previous_intervention": {
    "strategy": "reflective_listening",
    "objective": "帮助用户表达挫败感",
    "expected_signals": [
      "用户愿意补充经历"
    ]
  },
  "assistant_message": "...",
  "next_user_message": "我不想继续分析，我想知道怎么办"
}
```

### 输出

```json
{
  "observed_response": "用户拒绝继续情绪探索，并要求实际建议",
  "explicit_feedback": "negative",
  "objective_progress": "not_achieved",
  "strategy_fit": "poor",
  "recommended_adjustment": "switch_to_collaborative_problem_solving",
  "confidence": 0.93
}
```

### 允许判断的内容

- 用户是否接受当前方向；
- 用户是否明确纠正系统；
- 当前目标是否推进；
- 是否应继续、切换或暂停策略。

### 不允许判断的内容

- “治疗成功”；
- “心理状态已经改善”；
- “用户被治愈”；
- 任何长期临床效果结论。

### 生产—消费关系

- **生产者**：上一轮 intervention、当前用户消息；
- **消费者**：Intervention Ledger、State Reducer、Strategy Planner、跨会话策略统计。

---

## 4.6 Memory Retriever

### 类型

服务模块，可包含 embedding、reranker 和规则过滤。

### 目的

从长期状态与历史事件中检索本轮真正相关的信息。

### 输入

- `user_id`
- 当前消息；
- 当前 Session State；
- 当前主题；
- 权限与敏感级别；
- token 预算。

### 输出

```json
{
  "semantic_memories": [],
  "episodic_memories": [],
  "active_goals": [],
  "interaction_preferences": [],
  "previous_session_summary": null
}
```

### 检索优先级

1. 当前 active goal；
2. 上次未完成事项；
3. 与当前主题直接相关的历史事件；
4. 用户确认过的交流偏好；
5. 多次验证过的有效/无效策略。

### 不应自动检索

- 与当前主题无关的敏感历史；
- 未确认的模型心理推测；
- 已失效或已被替代的记忆；
- 其他用户的数据。

---

## 4.7 Context Builder

### 类型

确定性程序模块。

### 目的

按照优先级与 token 预算构建本轮主模型输入。

### 推荐 Context 顺序

```text
1. 系统边界与产品规则
2. 当前风险状态
3. 当前 Session State
4. Strategy Plan
5. Rolling Summary
6. 最近若干轮原始消息
7. 本轮相关长期记忆
8. 必要知识库内容
9. 输出格式要求
```

### 核心规则

- 当前原话优先于历史摘要；
- 新信息优先于旧信息；
- 用户陈述优先于模型推测；
- 只注入本轮相关记忆；
- 摘要是压缩缓存，不是事实真相；
- Context Builder 不修改数据库。

---

## 4.8 Strategy Planner

### 目的

选择下一轮采用的对话策略，而不是直接生成最终回复。

### 输入

- 当前 Session State；
- 当前风险状态；
- 最近 intervention 及其 feedback；
- 相关长期记忆；
- 会话阶段。

### 输出

```json
{
  "conversation_phase": "problem_solving",
  "primary_strategy": "collaborative_problem_solving",
  "objective": "帮助用户选择一个低压力沟通步骤",
  "reason": "用户明确要求实际建议",
  "avoid": [
    "继续深挖情绪",
    "一次提供过多建议",
    "替用户做决定"
  ],
  "expected_signals": [
    "用户能比较不同方案",
    "用户提出可接受的小步骤"
  ],
  "switch_conditions": [
    "用户拒绝行动",
    "情绪明显升级",
    "出现安全风险"
  ]
}
```

### 第一版策略集合

- `reflective_listening`
- `clarification`
- `emotional_exploration`
- `summarization`
- `psychoeducation`
- `collaborative_problem_solving`
- `action_planning`
- `progress_check`
- `safety_check`
- `session_closing`

### 生产—消费关系

- **生产者**：Session State、Feedback、Memory；
- **消费者**：Response Agent、Intervention Ledger、Context Builder。

---

## 4.9 Response Agent

### 目的

根据已确定的状态、策略与边界，生成自然语言回复。

### 输入

由 Context Builder 生成的完整 context。

### 输出

- 候选回复文本；
- 可选的结构化元数据，例如：
  - 是否提出问题；
  - 是否包含行动建议；
  - 是否引用了历史记忆。

### 约束

Response Agent 不负责：

- 永久写入记忆；
- 修改风险等级；
- 修改 Session State；
- 判断自己是否有效；
- 直接调用未授权敏感数据；
- 自主跳过安全流程。

---

## 4.10 Output Guard

### 目的

在回复发送前进行安全与边界检查。

### 检查内容

- 是否擅自诊断；
- 是否给出药物建议；
- 是否制造依赖；
- 是否越过风险流程；
- 是否错误引用用户历史；
- 是否泄露敏感信息；
- 是否与当前策略和状态冲突；
- 是否包含绝对化判断。

### 输出

```json
{
  "decision": "allow",
  "violations": [],
  "rewritten_response": null
}
```

允许结果：

- `allow`
- `rewrite`
- `block`
- `route_to_safety`

---

## 4.11 Rolling Summarizer

### 目的

压缩单次长会话中的较早对话。

### 触发条件

- 未摘要消息超过指定轮数；
- 当前 context 超过 token 阈值；
- 话题明显切换；
- 会话即将结束；
- 用户离开较长时间后返回。

### 输出

```json
{
  "summary_version": 4,
  "covered_from": "msg_001",
  "covered_to": "msg_080",
  "current_problem": "...",
  "session_goal": "...",
  "important_user_statements": [],
  "strategies_attempted": [],
  "strategy_responses": [],
  "open_questions": [],
  "source_message_ids": []
}
```

### 约束

- 不得新增用户未表达过的事实；
- 重要结论必须包含原始消息引用；
- 冲突信息必须显式标记；
- 定期从原始消息重建，避免摘要漂移。

---

## 4.12 Session Finalizer

### 目的

在一次会话结束时生成跨会话可使用的结构化总结。

### 输出

```json
{
  "session_summary": {},
  "goal_updates": [],
  "unfinished_topics": [],
  "action_items": [],
  "candidate_memories": [],
  "strategy_outcomes": [],
  "risk_events": []
}
```

### 消费者

- Long-Term State Updater；
- Memory Curator；
- 下一次 Session Bootstrap；
- 离线评测系统。

---

## 4.13 Memory Curator

### 目的

从会话内容中提出候选长期记忆，并处理去重、冲突、替代和过期。

### 输入

- 本轮原始消息；
- Session Finalizer 结果；
- 现有长期记忆；
- 用户授权设置；
- Memory Policy。

### 输出

```json
{
  "candidate_type": "interaction_preference",
  "content": "用户不喜欢一次收到大量建议",
  "source_message_ids": [
    "msg_103"
  ],
  "source_type": "explicit_user_statement",
  "confidence": 0.95,
  "requires_user_confirmation": false,
  "sensitivity": "medium",
  "recommended_operation": "CREATE"
}
```

### 合法操作

- `CREATE`
- `REINFORCE`
- `SUPERSEDE`
- `MARK_CONFLICT`
- `EXPIRE`
- `DELETE`

### 约束

Memory Curator 只能生成候选操作，最终写入由 Memory Policy 与 Memory Store 完成。

---

## 4.14 Memory Policy

### 类型

确定性规则模块。

### 目的

控制长期记忆是否允许被创建、读取、更新或删除。

### 决策因素

- 用户是否授权；
- 信息是否敏感；
- 来源是用户陈述还是模型推测；
- 是否需要用户确认；
- 是否存在重复；
- 是否与旧记忆冲突；
- 是否达到多次证据阈值；
- 当前模块是否有权限访问。

---

## 4.15 Audit Logger

### 目的

记录系统为何产生当前行为。

### 每轮应记录

- 输入消息 ID；
- Risk Agent 输出；
- State Delta；
- State Reducer 结果；
- 检索到的记忆 ID；
- Strategy Plan；
- Response Agent 模型版本；
- Output Guard 结果；
- Intervention ID；
- 后续 Feedback；
- 所有调用耗时与错误。

Audit Logger 是未来排查“模型为什么这样回复”的关键模块。

---

## 5. 状态分层

## 5.1 Turn State

只描述当前一轮：

```json
{
  "message_id": "msg_103",
  "user_intent": "request_actionable_advice",
  "current_topic": "工作冲突",
  "explicit_emotions": [
    "焦虑",
    "委屈"
  ],
  "corrections": [],
  "refusals": [
    "不想继续分析情绪"
  ]
}
```

## 5.2 Session State

描述本次咨询当前进展：

```json
{
  "session_id": "session_20",
  "phase": "problem_solving",
  "session_goal": "确定是否以及如何与直属领导沟通",
  "active_topics": [],
  "reported_emotions": [],
  "user_preferences": [],
  "open_questions": [],
  "pending_action_plan": null,
  "risk_state": {
    "level": "low"
  }
}
```

## 5.3 Rolling Summary

压缩较早对话，供当前 Session 使用。

## 5.4 Long-Term State

保存跨会话仍然有效、且经过授权的信息：

- active goals；
- interaction preferences；
- recurring topics；
- helpful strategies；
- unhelpful strategies；
- unfinished topics。

## 5.5 Intervention Ledger

保存每次策略、目标、预期信号和用户反馈。

---

## 6. 记忆分类

### 6.1 Working Memory

最近若干轮原始消息，只在当前 Session 使用。

### 6.2 Episodic Memory

具体历史事件，例如：

> 上次用户计划与直属领导安排一次一对一沟通。

### 6.3 Semantic Memory

相对稳定的用户信息，例如：

> 用户不喜欢一次收到大量建议。

### 6.4 Procedural Memory

对某位用户更合适的互动方式，例如：

> 用户情绪较强时，先倾听再讨论行动通常更合适。

Procedural Memory 至少需要多次证据，不应由单次互动直接建立。

---

## 7. 单轮生产—消费链路

```text
用户消息
  ↓
Message Store 保存原始消息
  ↓
Feedback Evaluator
  ├── 消费：上一轮 intervention + 当前用户消息
  └── 生产：feedback_result
  ↓
Risk Agent
  ├── 消费：当前消息 + 当前风险状态
  └── 生产：risk_result
  ↓
State Tracker
  ├── 消费：当前消息 + Session State + feedback_result
  └── 生产：state_delta
  ↓
State Reducer
  ├── 消费：旧 Session State + state_delta + risk_result
  └── 生产：新 Session State
  ↓
Memory Retriever
  ├── 消费：当前消息 + 新 Session State
  └── 生产：相关长期记忆
  ↓
Strategy Planner
  ├── 消费：Session State + feedback + memory
  └── 生产：strategy_plan
  ↓
Context Builder
  ├── 消费：规则 + 状态 + 摘要 + 最近消息 + 记忆 + strategy
  └── 生产：model_context
  ↓
Response Agent
  ├── 消费：model_context
  └── 生产：draft_response
  ↓
Output Guard
  ├── 消费：draft_response + risk_result + policy
  └── 生产：final_response
  ↓
Message Store 保存回复
  ↓
Intervention Store 创建 pending intervention
  ↓
异步 Post-Turn Pipeline
```

---

## 8. 会话内 Loop

```text
用户输入
  ↓
评估上一轮策略效果
  ↓
更新状态
  ↓
重新选择策略
  ↓
生成回复
  ↓
创建新的 pending intervention
  ↓
等待用户下一轮反馈
```

建议会话阶段：

- `OPENING`
- `EXPLORATION`
- `GOAL_ALIGNMENT`
- `INTERVENTION`
- `PROGRESS_CHECK`
- `CLOSING`

任何阶段都可跳转到：

- `SAFETY_CHECK`
- `CRISIS_PROTOCOL`
- `HUMAN_ESCALATION`

---

## 9. 跨会话 Loop

```text
Session N 结束
  ↓
Session Finalizer
  ↓
更新 goals、unfinished topics、strategy outcomes
  ↓
Memory Curator 生成候选长期记忆
  ↓
Memory Policy 审核
  ↓
写入 Long-Term State
  ↓
Session N+1 开始
  ↓
读取上次会话摘要、active goals、未完成事项、相关历史
  ↓
由用户决定继续旧话题或开始新话题
```

下一次会话不应默认用户一定想继续旧问题。

---

## 10. 异步 Post-Turn Pipeline

回复用户后，后台执行：

1. 更新 Rolling Summary；
2. 生成候选长期记忆；
3. 检查记忆冲突；
4. 更新 intervention ledger；
5. 记录模型与模块调用日志；
6. 生成可用于离线评测的样本；
7. 检查是否需要用户确认某条长期记忆。

---

## 11. Tool Calling 的使用边界

### 允许由模型发起的只读工具

- `retrieve_relevant_memory`
- `get_active_goals`
- `get_previous_session_summary`
- `retrieve_psychology_knowledge`

### 不允许模型直接执行的写操作

- 永久写入长期记忆；
- 删除记忆；
- 修改风险状态；
- 跳过输出审核；
- 访问未授权敏感数据；
- 在线微调模型。

模型可以提出写操作候选，但必须经过 Orchestrator 与 Policy 模块。

---

## 12. 第一版实现范围

第一版建议实现：

1. Turn Orchestrator；
2. Risk Agent；
3. State Tracker；
4. State Reducer；
5. Session State；
6. Rolling Summary；
7. Strategy Planner；
8. Response Agent；
9. Output Guard；
10. Feedback Evaluator；
11. Intervention Ledger；
12. Session Finalizer；
13. 少量经过授权的长期记忆。

第一版暂不实现：

- 复杂知识图谱；
- 模型参数在线更新；
- 全自动长期心理画像；
- 大规模多 Agent 讨论；
- 视觉、AU、心率驱动的状态更新；
- 完全自动危机咨询。

---

## 13. 关键验收标准

### 状态

- 不重复询问已经明确回答的问题；
- 用户纠正后，旧状态被正确标记为失效或冲突；
- 模型推测不会变成用户事实；
- 每个重要状态可追溯到消息 ID。

### 摘要

- 不新增原文不存在的事实；
- 重要结论有来源；
- 多次滚动后不明显漂移；
- 能保留当前目标、已尝试策略和未完成问题。

### 记忆

- 只保存允许保存的信息；
- 用户可以查看、修改和删除；
- 不跨用户污染；
- 当前状态优先于旧记忆。

### Loop

- 能根据显式负面反馈切换策略；
- 不把用户继续聊天当作“干预成功”；
- 不连续重复已被拒绝的策略；
- 不进行在线参数更新。

### 安全

- 风险路径不被普通主模型覆盖；
- 高风险情况下走受控流程；
- 不给出诊断和药物建议；
- 不产生依赖性表达。

---

## 14. 最终架构原则

> 对话模型不是系统本身。  
> 系统的核心是：可追溯的状态、受控的记忆、明确的策略循环，以及由程序掌握控制权的 Orchestrator。
