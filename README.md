<p align="center">
  <h1 align="center">📡 Broadcast Parsing Engine</h1>
  <p align="center">
    <strong>Real-Time NLP Trade Message Parser · Deterministic Holdings Engine · Human-in-the-Loop Safety</strong>
  </p>
  <p align="center">
    <img src="https://img.shields.io/badge/python-3.12-blue?style=flat-square&logo=python" alt="Python 3.12">
    <img src="https://img.shields.io/badge/scikit--learn-1.9-orange?style=flat-square&logo=scikit-learn" alt="scikit-learn">
    <img src="https://img.shields.io/badge/FastAPI-0.111-009688?style=flat-square&logo=fastapi" alt="FastAPI">
    <img src="https://img.shields.io/badge/tests-247_passing-brightgreen?style=flat-square" alt="Tests">
    <img src="https://img.shields.io/badge/docker-compose-2496ED?style=flat-square&logo=docker" alt="Docker">
    <img src="https://img.shields.io/badge/license-MIT-lightgrey?style=flat-square" alt="License">
  </p>
</p>

---

## What This Does

A production-grade **hybrid NLP pipeline** that parses unstructured financial trade broadcast messages (entries, exits, profit-booking, stop-loss triggers, reversals) into structured, validated trade events — then applies them to a **deterministic portfolio holdings engine** with exact `Decimal` arithmetic, **atomic multi-leg transactions**, and **human-in-the-loop (HITL) safety controls**.

> **Think of it as:** *a domain-specific NLP microservice that turns noisy Telegram/WhatsApp trading alerts into safe, auditable portfolio state changes — with zero floating-point drift and zero unreviewed mutations.*

---

## Architecture

```
                          ┌────────────────────────────────┐
                          │     Raw Broadcast Message      │
                          └────────────┬───────────────────┘
                                       ▼
                   ┌───────────────────────────────────────┐
                   │         Text Normalisation Layer      │
                   │   Unicode · whitespace · dash cleanup │
                   └───────────────────┬───────────────────┘
                                       ▼
              ┌────────────────────────────────────────────────┐
              │            Hybrid Classification Engine         │
              │                                                │
              │  ┌──────────────┐     ┌─────────────────────┐  │
              │  │ Rule Engine  │     │  ML Classifier       │  │
              │  │ 27 regex     │     │  TF-IDF (word+char)  │  │
              │  │ rules        │ ──▶ │  Logistic Regression │  │
              │  │ deterministic│     │  with calibrated     │  │
              │  │ override     │     │  confidence scores   │  │
              │  └──────────────┘     └─────────────────────┘  │
              └────────────────────────┬───────────────────────┘
                                       ▼
                   ┌───────────────────────────────────────┐
                   │       Entity Extraction Layer         │
                   │  Symbols · Prices · Targets · SL ·    │
                   │  Strike · Expiry · Multi-instrument   │
                   └───────────────────┬───────────────────┘
                                       ▼
                   ┌───────────────────────────────────────┐
                   │         Validation & Safety Layer     │
                   │  Confidence gates · Idempotency ·     │
                   │  Direction conflict · Missing symbol  │
                   │  Multi-instrument blocking ·          │
                   │  Unsafe auto-apply prevention         │
                   └───────────────────┬───────────────────┘
                                       ▼
         ┌─────────────────────────────────────────────────────┐
         │            Holdings Engine (Decimal-exact)           │
         │                                                     │
         │  Shadow Portfolio ◄──── compare ────► Verified      │
         │  (speculative)         (diff)         (approved)    │
         │                                                     │
         │  Atomic ordered close-then-entry · Rollback safety  │
         └─────────────────────────────────────────────────────┘
                                       ▼
                   ┌───────────────────────────────────────┐
                   │        Human Review Interface         │
                   │   Streamlit dashboard · Approve ·     │
                   │   Reject · Edit-and-Approve ·         │
                   │   Active learning label export        │
                   └───────────────────────────────────────┘
```

---

## Key Features

### 🧠 Hybrid NLP Pipeline
- **Deterministic rule engine** (27 regex rules) handles unambiguous patterns with 100% precision
- **scikit-learn ML classifier** (TF-IDF word/char n-grams → Logistic Regression) handles ambiguous or novel phrasings
- **Resolution priority**: Rule > ML when rules fire; ML primary when rules are inconclusive; conflicts → human review
- **Regex entity extraction**: symbols, execution prices, stop-loss, targets, strike prices, expiry months, option types

### 💰 Exact-Decimal Holdings Engine
- **`Decimal` arithmetic** — no IEEE 754 floating-point drift in financial calculations
- **Compounding reduction formula**: `new = current × (1 − reduction% / 100)`
  - `100 → 50 → 25 → 18.75` — exact at every step
- **7-field composite position identity**: `(portfolio, market, symbol, contract_month, option_type, strike, direction)`
- **Atomic ordered close-then-entry**: multi-leg reversal transactions execute atomically or roll back entirely

### 🔒 Safety & Governance
- **Triple portfolio isolation**: Historical, Shadow (speculative), and Verified (human-approved) — never cross-contaminated
- **Human-in-the-loop (HITL)**: Verified holdings change **only** after explicit human approval
- **Confidence gating**: Low-confidence ML predictions route to manual review, never auto-applied
- **Validation guards**: Missing symbols, conflicting directions, ambiguous sells, multi-instrument collisions — all blocked before state mutation
- **Idempotent message processing**: Duplicate ingestion is safely rejected

### 📊 Active Learning & Observability
- **Shadow testing**: Parser runs against live data in shadow mode; human reviewers compare predictions against verified portfolio diffs
- **Label export pipeline**: Reviewer corrections automatically produce labeled training samples for model retraining
- **Historical replay**: Auditable replay engine validated across 1,000+ chronological messages
- **Structured audit trail**: Every event, snapshot, and review decision is persisted in an immutable ledger

### 🌐 REST API (FastAPI)
- `POST /api/v1/parse` — Parse a raw message → structured validated event
- `GET /api/v1/holdings` — Query current portfolio positions
- `GET /api/v1/health` — Liveness probe with DB connectivity check
- Interactive Swagger docs at `/docs`

### 🐳 Containerised Deployment
- `docker compose up` launches the API server (`:8000`) and Streamlit review UI (`:8501`)
- Shared SQLite volume for data consistency between services

---

## Tech Stack

| Layer | Technology |
|:---|:---|
| Language | Python 3.12 |
| ML / NLP | scikit-learn, TF-IDF, Logistic Regression, regex |
| API | FastAPI, Uvicorn, Pydantic |
| Review UI | Streamlit |
| Storage | SQLite (WAL mode, foreign keys, Decimal-as-TEXT) |
| Containerisation | Docker, Docker Compose |
| Testing | pytest (247 tests), pytest-cov |
| Arithmetic | Python `decimal.Decimal` — exact base-10 |

---

## Quick Start

### Option A: Docker (Recommended)

```bash
git clone https://github.com/YashChoubey06/Broadcast-Parsing-Engine.git
cd Broadcast-Parsing-Engine

docker compose up --build
```

- **API**: http://localhost:8000/docs (interactive Swagger)
- **Review UI**: http://localhost:8501

### Option B: Local Development

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
# .venv\Scripts\activate   # Windows

# Install dependencies
pip install -r requirements.txt

# Initialise database
python -m src.database

# Run tests (247 passing)
python -m pytest tests/ -v

# Start the API server
uvicorn src.api:app --reload --port 8000

# Start the review UI (separate terminal)
streamlit run app/shadow_review_app.py
```

---

## API Usage Examples

### Parse a Trade Message

```bash
curl -X POST http://localhost:8000/api/v1/parse \
  -H "Content-Type: application/json" \
  -d '{
    "raw_text": "BUY 50% TSLA @250 SL 240 TGT 270",
    "source_message_id": "broadcast-001",
    "segment": "GLOBAL_EQUITY"
  }'
```

**Response:**
```json
{
  "source_message_id": "broadcast-001",
  "parser_version": "trade-parser-v1",
  "prediction_type": "event",
  "validation_status": "VALID",
  "needs_review": false,
  "auto_apply_eligible": true,
  "event": {
    "final_action": "OPEN_LONG",
    "symbol": "TSLA",
    "direction": "LONG",
    "quantity_percent": "50",
    "execution_price_primary": "250",
    "stop_loss": "240",
    "targets": ["270"],
    "resolution_source": "RULE"
  }
}
```

### Query Portfolio Holdings

```bash
curl http://localhost:8000/api/v1/holdings?portfolio_id=default
```

---

## Project Structure

```
├── src/
│   ├── api.py                   # FastAPI REST endpoints
│   ├── hybrid_parser.py         # Hybrid rule + ML classification engine
│   ├── rule_parser.py           # Deterministic regex rule engine (27 rules)
│   ├── ml_classifier.py         # TF-IDF + Logistic Regression classifier
│   ├── entity_extractor.py      # Regex entity extraction (symbols, prices, SL, targets)
│   ├── validator.py             # Pre-engine validation & safety guards
│   ├── holdings_engine.py       # Decimal-exact portfolio state machine
│   ├── ordered_event_service.py # Atomic close-then-entry reversal handling
│   ├── shadow_service.py        # Shadow/verified portfolio isolation service
│   ├── position_identity.py     # 7-field composite instrument identity resolver
│   ├── database.py              # SQLite connection management & schema DDL
│   ├── sqlite_repository.py     # Repository pattern over SQLite
│   └── ...                      # CLI tools, config, schemas, exports
├── app/
│   ├── shadow_review_app.py     # Streamlit HITL review dashboard
│   └── phase4_candidate_review_app.py
├── tests/                       # 247 automated tests
├── models/                      # Trained ML model artifacts
├── data/                        # Training datasets & instrument aliases
├── Dockerfile
├── docker-compose.yml
└── pyproject.toml
```

---

## Testing

```bash
# Full suite (247 tests, ~7s)
python -m pytest tests/ -v

# Specific modules
python -m pytest tests/test_holdings_engine.py -v       # Decimal arithmetic, reductions
python -m pytest tests/test_shadow_workflow.py -v       # HITL safety, portfolio isolation
python -m pytest tests/test_ordered_reversal.py -v      # Atomic close-then-entry
python -m pytest tests/test_hybrid_parser.py -v         # NLP classification
python -m pytest tests/test_position_identity.py -v     # Instrument identity resolution
```

Test coverage includes:
- **Decimal arithmetic correctness** — compound reductions, allocation chains, zero-tolerance thresholds
- **Idempotent message processing** — duplicate ingestion rejection
- **Portfolio isolation** — shadow never contaminates verified
- **Atomic transactions** — ordered reversal rollback on partial failure
- **Validation edge cases** — missing symbols, conflicting directions, multi-instrument blocking
- **Safety invariants** — invalid predictions cannot be approved, low-confidence gating

---

## Design Decisions

| Decision | Rationale |
|:---|:---|
| `Decimal` over `float` | IEEE 754 binary representation errors (`0.1 + 0.2 ≠ 0.3`) cause cumulative financial drift. `Decimal` guarantees exact base-10 arithmetic. |
| Rule engine > LLM API | Deterministic rules provide microsecond latency, zero cost, 100% reproducibility, and no hallucination risk on financial numbers. ML handles only ambiguous cases. |
| Shadow portfolio | Acts as a staging environment for live inference — measure model drift and compare predictions against verified holdings without risking real capital. |
| SQLite with TEXT columns | Decimal values stored as TEXT strings preserve exact representation. `REAL` columns would silently convert to IEEE 754 floats. |
| Atomic ordered execution | Multi-leg close-then-entry transactions must fully succeed or fully roll back. Partial execution would leave corrupted portfolio state. |
| HITL approval gate | Model accuracy (~82%) is insufficient for unattended financial state changes. Every prediction requires human verification. |

---

## Model Performance

| Metric | Value |
|:---|:---|
| Accuracy | 82.2% |
| Weighted F1 | 0.84 |
| Macro F1 | 0.54 |
| Training data | TF-IDF (word 1-2 gram + char 3-5 gram) |
| Classifier | Logistic Regression (calibrated probabilities) |

> The model is intentionally **not** approved for unattended production updates. The hybrid architecture ensures deterministic rules handle high-frequency patterns with 100% precision, while ML fills gaps for novel phrasings — all gated by human review.

---

## Roadmap

- [ ] Node.js / React integration for production frontend
- [ ] PostgreSQL migration for multi-user concurrency
- [ ] CI/CD pipeline with automated test gating
- [ ] Model retraining with accumulated human-reviewed labels
- [ ] Multi-instrument message splitting into child events
- [ ] WebSocket streaming for real-time broadcast ingestion

---

## License

MIT
