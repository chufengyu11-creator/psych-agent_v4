# Evaluation Datasets

这些 JSON 是离线、确定性的 evaluation case，不是运行时公共 schema。

- `adaptive_loop_cases.json`：`input.user_turns` 是送入真实 `TurnOrchestrator` 的用户输入；`expected` 是 feedback、strategy 或 state 的机器可判定约束。runner 使用现有 FakeRiskAgent、FakeStateTracker、FakeFeedbackEvaluator、FakeStrategyPlanner、FakeResponseAgent、FakeOutputGuard、StateReducer 和内存 repositories。
- `summary_cases.json`：`input.messages/current_state/interventions` 会转换为 typed objects 后交给 `FakeRollingSummarizer`；`expected` 只用于评价生成的 `RollingSummary`。
- `memory_cases.json`：`kind=curator` 的 `input.messages` 交给 `FakeMemoryCurator`；`kind=policy` 的 `input.candidate` 交给真实确定性 `MemoryPolicy`。`expected` 从不充当 actual candidate。
- `safety_cases.json`：`input.current_message/recent_messages/current_risk_level` 会转换为 `Message` 和 `RiskInput`，再交给现有 `FakeRiskAgent`；`expected_risk` 可约束允许的风险级别和路由、类别包含/精确/排除、澄清标志、reason code 与最低置信度。evaluator 还会拒绝高风险或危险类别进入 normal route，以及拒绝没有澄清标志的 crisis/human route。

所有 `input` 字段是被测组件的输入，所有 `expected` 字段仅由 evaluator 读取。默认 runner 不调用真实 LLM、数据库、Redis、SQLite、HTTP 或 FastAPI。Safety suite 通过只证明当前 fake 实现满足这些离线确定性 case，不代表真实模型已完成临床验证或达到生产医疗安全标准。
