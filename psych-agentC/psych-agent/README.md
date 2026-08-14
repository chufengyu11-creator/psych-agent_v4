# Psych Support Agent Base

This repository currently contains the first thin base for a psychological-support dialogue agent. It is not a therapy or diagnosis system; the first milestone is a typed, auditable orchestration skeleton that later teams can replace with real storage and LLM implementations.

## What works now

- Typed public contracts in `schemas/` for messages, risk, state, feedback, strategy, context, safety, memory, summaries, and interventions.
- Fake agent implementations in `agents/` that satisfy the same interfaces expected from real model-backed agents.
- A deterministic `TurnOrchestrator` that controls one full user-turn lifecycle.
- In-memory repositories so the fake pipeline can run before PostgreSQL models exist.
- A minimal FastAPI app with `GET /health` and `POST /chat/turn`.
- Integration tests for the fake single-turn and adaptive-feedback loop.

## Run locally

```bash
python -m pytest
uvicorn app.main:app --reload
```

Example request:

```bash
curl -X POST http://127.0.0.1:8000/chat/turn \
  -H "Content-Type: application/json" \
  -d '{"user_id":"user_1","session_id":"session_1","message":"我今天有点焦虑，想知道怎么做。"}'
```

## Design notes

The orchestrator owns control flow. Agents produce typed candidates; they do not write state, mutate memory, skip safety routing, or directly persist user data. `StateReducer` is deterministic and is the only component that turns `StateDelta` into a new `SessionState` version.

For file-by-file responsibilities, see `docs/contracts/README.md`.
