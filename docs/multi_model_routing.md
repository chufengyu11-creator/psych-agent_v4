# Subagent 多模型路由说明

## 1. 当前问题

一轮正常对话不是只调用一次模型。现有编排会依次执行：

1. `RiskAgent`：风险分类；
2. `StateTracker`：抽取状态变化；
3. `StrategyPlanner`：选择对话策略；
4. `ResponseAgent`：生成用户可见回复；
5. `OutputGuard`：审核回复；
6. `RollingSummarizer`：更新滚动摘要。

从第二轮开始，如果存在上一轮待评估干预，还会增加一次 `FeedbackEvaluator` 调用。因此，“同一轮多次调用模型”是多 Agent 编排产生的，不是同一个 HTTP 请求被无条件重复发送。

旧实现只有一个 `LLM_BASE_URL`。即使不同 Agent 在请求中填写不同 `model_name`，请求仍然会发送到同一个本地推理服务。不同参数量模型若分别运行在不同端口，就无法真正切换模型。

## 2. 新路由方式

新增两层映射：

- Agent -> 模型名：由 `RISK_MODEL_NAME`、`STATE_MODEL_NAME` 等变量决定；
- 模型名 -> 服务地址：由 `LLM_MODEL_ROUTES` 决定。

调用链变为：

```text
RiskAgent -> local-small -> http://127.0.0.1:8101/v1
StateTracker -> local-medium -> http://127.0.0.1:8102/v1
ResponseAgent -> local-large -> http://127.0.0.1:8103/v1
```

`RoutedLLMClient` 根据 `LLMRequest.model_name` 选择对应的 HTTP 客户端。各 Agent 的业务代码、Prompt、Pydantic 输出结构和编排顺序都不需要修改。

## 3. 推荐的任务分配

| Subagent | 任务性质 | 建议模型级别 |
|---|---|---|
| `RiskAgent` | 分类、路由、风险标签 | 小模型，但必须专项验证召回率 |
| `StateTracker` | JSON 信息抽取、状态更新 | 中模型 |
| `FeedbackEvaluator` | 反馈分类 | 小模型 |
| `StrategyPlanner` | 多约束策略决策 | 中模型 |
| `ResponseAgent` | 最终自然语言生成 | 大模型 |
| `OutputGuard` | 审核、违规分类、必要时改写 | 小或中模型；不能只追求速度 |
| `RollingSummarizer` | 压缩历史上下文 | 小或中模型 |
| `SessionFinalizer` | 会话结束总结 | 中模型 |
| `MemoryCurator` | 长期记忆筛选与结构化 | 中模型 |

这只是部署起点。最终应根据各 Agent 的离线数据集分别评估准确率、JSON 成功率、延迟和显存占用，再决定参数量。

## 4. `.env` 示例

```dotenv
APP_RUNTIME_MODE=sqlalchemy_model

LLM_API_KEY=local-development-key
LLM_BASE_URL=http://127.0.0.1:8102/v1

MAIN_MODEL_NAME=local-large
STRUCTURED_MODEL_NAME=local-medium
SAFETY_MODEL_NAME=local-small

LLM_MODEL_ROUTES={"local-small":"http://127.0.0.1:8101/v1","local-medium":"http://127.0.0.1:8102/v1","local-large":"http://127.0.0.1:8103/v1"}

RISK_MODEL_NAME=local-small
STATE_MODEL_NAME=local-medium
FEEDBACK_MODEL_NAME=local-small
STRATEGY_MODEL_NAME=local-medium
RESPONSE_MODEL_NAME=local-large
OUTPUT_GUARD_MODEL_NAME=local-small
SUMMARY_MODEL_NAME=local-small
SESSION_FINALIZER_MODEL_NAME=local-medium
MEMORY_CURATOR_MODEL_NAME=local-medium
```

模型名必须与对应推理服务 `GET /v1/models` 返回的 `id` 完全一致。这里的 `local-small`、`local-medium`、`local-large` 是示例服务名，不是固定模型。

## 5. 兼容行为

- `LLM_MODEL_ROUTES` 为空时，所有模型仍使用原来的 `LLM_BASE_URL`，旧部署方式不受影响。
- 某个 Agent 专属变量为空时，会回退到原有的三类默认模型：
  - 回复类回退到 `MAIN_MODEL_NAME`；
  - 结构化任务回退到 `STRUCTURED_MODEL_NAME`；
  - 风险和审核回退到 `SAFETY_MODEL_NAME`。
- 路由中缺少请求模型时直接抛出配置错误，不会静默切回其他模型。
- `/health/ready` 会检查当前 Agent 图所需的全部模型是否能在各端点的 `/models` 中发现。

## 6. 验证方法

启动所有本地模型服务和 Agent 后：

1. 请求每个模型服务的 `/v1/models`，确认模型 ID 与 `.env` 一致；
2. 请求 Agent 的 `/health/ready`，确认 `llm.status=ok`；
3. 连续对话两轮，查看模型服务访问日志；
4. 应看到风险、反馈、摘要等请求进入小模型端口，状态和策略进入中模型端口，最终回复进入大模型端口；
5. 对每个 Agent 单独统计 JSON 解析失败率和 fallback 次数。

## 7. 仍未减少的调用

本次改动解决的是“不同 Subagent 选择不同本地模型”，不会改变每轮 Agent 数量。若后续还要降低调用次数，应单独评估：

- 将 `RiskAgent` 与 `StateTracker` 合并为一次结构化输出；
- 仅在必要时运行 `OutputGuard`；
- 将 `RollingSummarizer` 从同步 inline 改成真正的异步任务；
- 通过规则先筛掉无需 `FeedbackEvaluator` 的轮次。

这些会改变编排语义、事务边界或安全策略，不应与本次模型路由改动混在一起。
