# Person C 完成交付说明

## 一、需要替换或新增的核心文件

### 1. LLM Client 层

- `llm/client.py`
  - OpenAI-compatible `/v1/chat/completions` 异步调用；
  - 支持 `LLMRequest -> LLMResponse`；
  - 支持 `generate_text()`；
  - 支持 `generate_structured()`；
  - 支持超时、HTTP 错误、传输错误、非法 JSON 和 Pydantic 校验失败重试；
  - 支持安全异常上抛，由各 Agent 执行 fallback。
- `llm/model_registry.py`
  - 管理模型端点；
  - 支持默认模型；
  - 支持 `agent_name -> model_name` 路由；
  - 支持不同 Agent 使用主模型、结构化模型或安全模型。
- `llm/retry_policy.py`
  - 指数退避和可重试状态码。
- `llm/structured_output.py`
  - 提取普通 JSON、Markdown fenced JSON；
  - Pydantic structured output 校验。
- `llm/exceptions.py`
  - 统一配置、HTTP、超时、结构化输出和重试耗尽异常。
- `llm/structured_client.py`
  - 保留原 `LLMClientProtocol` 适配方式，兼容主项目现有代码。
- `llm/prompt_renderer.py`
  - 将 `ResponseContext` 渲染为稳定的 Chat Completions 请求；
  - 接入 `prompts/response_agent.md`。

### 2. 五个 Online Agent

- `agents/risk_agent.py`
  - 保留 `FakeRiskAgent`；
  - `RiskAgent` 输出 `RiskResult`；
  - 模型失败时进入保守安全路由；
  - 明显自伤/他伤关键词具有不可绕过的安全下限。
- `agents/state_tracker.py`
  - 保留 `FakeStateTracker`；
  - `StateTracker` 只输出 `StateDelta`；
  - 模型失败返回最小安全 delta；
  - 所有状态证据重新绑定到当前真实 `message_id`，避免模型伪造来源 ID。
- `agents/strategy_planner.py`
  - 保留 `FakeStrategyPlanner`；
  - `StrategyPlanner` 输出 `StrategyPlan`；
  - 负反馈和 poor-fit 时切换策略；
  - 非普通风险路由强制切换为 `safety_check`。
- `agents/response_agent.py`
  - 保留 `FakeResponseAgent`；
  - `ResponseAgent` 消费 `ResponseContext`，输出 `DraftResponse`；
  - 模型失败时根据策略生成安全 fallback；
  - 自动规范 `asked_question`、`contains_action_suggestion` 和 memory IDs。
- `agents/output_guard.py`
  - 保留 `FakeOutputGuard`；
  - 规则层 + 模型语义审查；
  - 阻止诊断、药物建议、疗效保证、AI 依赖、隔离现实支持、危险行动、过度心理解释和内部 Prompt 泄露；
  - 风险路由下普通对话强制 `route_to_safety`；
  - 模型失败默认 `block`。

### 3. Prompt 文件

- `prompts/risk_agent.md`
- `prompts/state_tracker.md`
- `prompts/strategy_planner.md`
- `prompts/response_agent.md`
- `prompts/output_guard.md`

五个 Prompt 均包含任务边界、输入字段、禁止行为、输出 schema、正确/错误示例和 fallback 规则。

### 4. 测试文件

- `tests/unit/test_llm_client.py`
- `tests/contract/test_risk_agent.py`
- `tests/contract/test_state_tracker.py`
- `tests/contract/test_strategy_planner.py`
- `tests/contract/test_response_and_guard.py`

## 二、推荐初始化方式

```python
from agents.output_guard import OutputGuard
from agents.response_agent import ResponseAgent
from agents.risk_agent import RiskAgent
from agents.state_tracker import StateTracker
from agents.strategy_planner import StrategyPlanner
from llm.client import LLMClient
from llm.model_registry import ModelConfig, ModelRegistry

registry = ModelRegistry(
    models=[
        ModelConfig(
            name="main-model",
            provider_model_name="your-main-model-name",
            endpoint="http://127.0.0.1:8000/v1/chat/completions",
            timeout_seconds=30.0,
        ),
        ModelConfig(
            name="safety-model",
            provider_model_name="your-safety-model-name",
            endpoint="http://127.0.0.1:8000/v1/chat/completions",
            timeout_seconds=20.0,
        ),
    ],
    default_model="main-model",
    agent_models={
        "risk_agent": "safety-model",
        "output_guard": "safety-model",
        "state_tracker": "main-model",
        "strategy_planner": "main-model",
    },
)

llm_client = LLMClient(registry)

risk_agent = RiskAgent(llm_client)
state_tracker = StateTracker(llm_client)
strategy_planner = StrategyPlanner(llm_client)
response_agent = ResponseAgent(llm_client, model_name="main-model")
output_guard = OutputGuard(llm_client)
```

`endpoint` 要填写完整的 OpenAI-compatible Chat Completions 地址，例如：

```text
http://127.0.0.1:8000/v1/chat/completions
```

## 三、验收命令

Person C 专项测试：

```bash
pytest -q \
  tests/unit/test_llm_client.py \
  tests/contract/test_risk_agent.py \
  tests/contract/test_state_tracker.py \
  tests/contract/test_strategy_planner.py \
  tests/contract/test_response_and_guard.py \
  tests/unit/test_model_backed_agents.py \
  tests/unit/test_structured_llm_client.py \
  tests/unit/test_response_agent.py \
  tests/integration/test_model_assisted_turn_flow.py
```

当前交付验证结果：

```text
28 passed
```

排除当前容器缺失数据库驱动的非数据库测试结果：

```text
105 passed
```

全量测试前请安装开发依赖：

```bash
pip install -e ".[dev]"
pytest -q
```

## 四、边界说明

本次没有修改以下禁止文件：

- `storage/`
- `orchestrator/turn_orchestrator.py`
- `services/state_reducer.py`

所有 Agent 均不直接访问数据库，不直接修改 `SessionState`，也不绕过 Orchestrator。
