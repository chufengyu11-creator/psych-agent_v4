# Contract And File Responsibilities

This document describes the first base that is safe for parallel development. Each function has a typed signature in code; this page explains what each file/class/function is responsible for.

## Public schema files

| File | Responsibility |
| --- | --- |
| `schemas/common.py` | Shared base model, typed IDs, timestamps, source references, and confidence score. `utc_now()` provides timestamp defaults. |
| `schemas/messages.py` | Raw append-only message types plus `ChatTurnRequest` and `ChatTurnResult`. |
| `schemas/risk.py` | Risk input/result contracts and the allowed safety routes. |
| `schemas/state.py` | Versioned `SessionState`, incremental `StateDelta`, update item types, and `StateTrackerInput`. |
| `schemas/intervention.py` | Pending/evaluated intervention ledger contract. |
| `schemas/feedback.py` | Feedback evaluator input and output contracts for adaptive strategy switching. |
| `schemas/memory.py` | Retrieved memory bundle and candidate memory operation contracts. |
| `schemas/summary.py` | Rolling summary contract for long-session compression. |
| `schemas/strategy.py` | Strategy planner input and output contracts. |
| `schemas/context.py` | Response context and named context sections. |
| `schemas/safety.py` | Draft response, output guard input/result, and final text selection logic. |

## Agent files

| File | Responsibility |
| --- | --- |
| `agents/base.py` | Defines generic `Agent` and `TaskQueue` protocols. |
| `agents/risk_agent.py` | `FakeRiskAgent.run/analyze()` returns a typed `RiskResult`; crisis keywords route to safety. |
| `agents/state_tracker.py` | `FakeStateTracker.run/extract_delta()` extracts simple topic, goal, emotion, and preference deltas. |
| `agents/feedback_evaluator.py` | `FakeFeedbackEvaluator.run/evaluate()` labels observable user feedback to the previous intervention. |
| `agents/strategy_planner.py` | `FakeStrategyPlanner.run/plan()` chooses a typed strategy from state and feedback. |
| `agents/response_agent.py` | `FakeResponseAgent.run/generate()` creates a typed draft response from `ResponseContext`. |
| `agents/output_guard.py` | `FakeOutputGuard.run/review()` blocks obvious boundary violations before sending. |

## Service files

| File | Responsibility |
| --- | --- |
| `services/token_budget.py` | Central token budget map and `TokenBudgetManager.budget_for()`. |
| `services/state_reducer.py` | `StateReducer.apply()` creates the next state version; helper methods apply topics, goals, emotions, preferences, and inactive markers. |
| `services/context_builder.py` | `ContextBuilder.build()` assembles ordered, typed context sections for the response agent. |
| `services/memory_retriever.py` | `MemoryRetriever.retrieve()` is the retrieval boundary and currently returns an empty typed memory bundle. |

## Orchestrator files

| File | Responsibility |
| --- | --- |
| `orchestrator/turn_orchestrator.py` | `TurnOrchestrator.handle_turn()` controls the complete turn lifecycle; private helpers evaluate previous interventions and retrieve memories. Protocol classes define required collaborator methods. |
| `orchestrator/safety_router.py` | `SafetyRouter.handle()` maps non-normal risk routes to controlled safety responses. |
| `orchestrator/post_turn_pipeline.py` | `NoopTaskQueue.enqueue()` accepts background task requests until real workers exist. |
| `orchestrator/exceptions.py` | Base orchestrator exception type. |

## Storage files

| File | Responsibility |
| --- | --- |
| `storage/repositories/message_repository.py` | Message repository protocol and `InMemoryMessageRepository` for append-only messages. |
| `storage/repositories/state_repository.py` | State repository protocol and `InMemoryStateRepository` for current state versions. |
| `storage/repositories/intervention_repository.py` | Intervention repository protocol and `InMemoryInterventionRepository` for pending/evaluated interventions. |
| `storage/repositories/summary_repository.py` | Summary repository protocol and `InMemorySummaryRepository`, currently returning no summary. |

## API and app files

| File | Responsibility |
| --- | --- |
| `app/main.py` | `create_app()` registers routers and exposes the FastAPI `app`. |
| `app/dependencies.py` | `get_turn_orchestrator()` wires fake agents, services, and in-memory repositories. |
| `api/routers/chat.py` | `handle_chat_turn()` accepts `POST /chat/turn` and calls the orchestrator. |
| `api/routers/health.py` | `health_check()` returns a minimal liveness response. |
| `api/request_models.py` / `api/response_models.py` | Compatibility modules; canonical models live in `schemas.messages`. |

## Tests

| File | Responsibility |
| --- | --- |
| `tests/integration/test_turn_flow.py` | Verifies fake pipeline response, state version updates, pending intervention creation, and second-turn feedback handling. |
