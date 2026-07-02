# Trade Message NLP Parser & Holdings Engine

A complete, locally-runnable Python project implementing a three-stage pipeline for processing trading broadcast messages:

- **Step 5** — Hybrid NLP + rule-based parser → structured `ParsedTradeEvent`
- **Step 6** — Deterministic holdings engine consuming only validated events
- **Step 7** — Historical replay over production messages with local SQLite persistence

---

## Architecture

```
Raw message
    ↓
[Step 5] text_normalizer → alias_repository → entity_extractor
                         → rule_parser (R01-R13)
                         → ml_classifier (scikit-learn LR)
                         → hybrid_parser (resolver)
                         → validator
    ↓
Structured ParsedTradeEvent
    ↓
[Step 6] holdings_engine (Decimal arithmetic)
         ← position_repository (interface)
         ← trade_event_repository (interface)
         ← processed_message_repository (idempotency)
         ← review_queue_repository
         ← snapshot_repository
    ↓
Updated holdings + immutable trade-event record
    ↓
[Step 7] SQLite persistence (storage/trade_holdings.db)
         Historical replay (01_production_messages_final.csv)
```

### Why the parser and engine are separate

The NLP model **never modifies holdings**. It produces a `ParsedTradeEvent`.
Only the deterministic engine reads positions and applies changes.
This means:
- A misclassification cannot corrupt positions.
- The engine is fully unit-testable without any ML dependency.
- Rules always override ML for deterministic phrases (SL touched, part profit, etc.).
- The ML model can be retrained or replaced without touching the engine.

---

## Installation

### Windows PowerShell

```powershell
cd "c:\Astrodunia text parsing\trade_message_system"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -e .
```

### macOS / Linux

```bash
cd "/path/to/trade_message_system"
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

---

## Dataset placement

All required CSV files are already in `data/`. They were copied from the source package:

```
data/
├── 01_production_messages_final.csv
├── 03_model_train_ready_deduplicated.csv
├── 04_annotation_review_queue.csv
├── 05_production_manual_or_context_queue.csv
├── 07_pilot_train_210.csv
├── 08_pilot_validation_45.csv
├── 09_pilot_test_45.csv
├── 10_instrument_aliases.csv
├── 11_label_and_position_rules.csv
└── 12_dataset_schema.csv
```

---

## Initialise the database

```powershell
python -m src.database
```

or

```powershell
python scripts/init_database.py
```

Database location: `storage/trade_holdings.db`

---

## Train the model

### Pilot files (recommended first step)

```powershell
python -m src.train_model `
  --train data/07_pilot_train_210.csv `
  --validation data/08_pilot_validation_45.csv `
  --test data/09_pilot_test_45.csv
```

### Full deduplicated dataset with auto-split

```powershell
python -m src.train_model `
  --train data/03_model_train_ready_deduplicated.csv `
  --auto-split
```

### With comparison models

```powershell
python -m src.train_model `
  --train data/07_pilot_train_210.csv `
  --validation data/08_pilot_validation_45.csv `
  --test data/09_pilot_test_45.csv `
  --compare
```

Model saved to: `models/action_classifier.joblib`

> **Label note**: Training labels are initial/weak (rule-assisted annotation), not fully human-verified gold-standard. Evaluation reports represent baseline performance.

---

## Evaluation reports

After training, reports are written to `reports/`:

| File | Contents |
|------|----------|
| `classification_report.json` | Accuracy, macro F1, weighted F1, per-class metrics |
| `classification_report.csv` | Same as above in CSV |
| `confusion_matrix.csv` | Class × class confusion matrix |
| `misclassified_examples.csv` | Rows where prediction ≠ true label |
| `low_confidence_predictions.csv` | Predictions below AUTO_APPLY_THRESHOLD |

---

## Parse one message

```powershell
python -m src.parse_message --text "50% PROFIT BOOK IN NIFTY @25942"
```

With holdings context (routes unsafe opposite-side standalone entries to review):

```powershell
python -m src.parse_message `
  --text "SELL 50% NIFTY" `
  --portfolio default `
  --use-db-context
```

---

## Apply one message

```powershell
python -m src.apply_message `
  --message-id test-001 `
  --text "BUY 50% NVDA @145 SL 140 TGT 155"
```

---

## Historical replay

### Dry-run (no database changes)

```powershell
python -m src.replay `
  --input data/01_production_messages_final.csv `
  --db storage/trade_holdings.db `
  --mode dry-run
```

Dry-run uses in-memory repositories; the real database is not touched.

### Safe-apply

```powershell
python -m src.replay `
  --input data/01_production_messages_final.csv `
  --db storage/trade_holdings.db `
  --mode safe-apply
```

Safe-apply only processes messages where:
- `auto_apply_eligible = True` (dataset)
- `needs_review = False` (dataset)
- `requires_context = False` (dataset)
- Fresh parser validation passes
- `source_message_id` has not been previously processed
- Parser action agrees with dataset label

Everything else goes to the manual-review queue.

### Reset the development database (DANGER)

```powershell
python -m src.replay --reset-database --confirm-reset
```

This drops and recreates all tables. Never run on production data.
Only databases inside `storage/` can be reset for safety.

---

## Inspect holdings

```powershell
python -m src.show_holdings
python -m src.show_holdings --all        # includes closed positions
```

## Inspect symbol history

```powershell
python -m src.show_history --symbol NIFTY
python -m src.show_history --symbol CRUDE
```

## Inspect review queue

```powershell
python -m src.show_review_queue
python -m src.show_review_queue --all
```

## Shadow review workflow

Shadow testing uses separate portfolios in the shared `positions` table:

| Portfolio | Purpose |
|-----------|---------|
| `default` / historical | Historical replay and standard local application |
| `shadow` | Parser predictions applied for comparison only |
| `verified` | Human-approved holdings state |

Ingested shadow messages are stored as immutable parser predictions first.
Safe parser predictions may update only the `shadow` portfolio. The `verified`
portfolio changes only after a human reviewer approves or edits the prediction.

Start the local review app:

```powershell
.\.venv\Scripts\python.exe -m streamlit run app/shadow_review_app.py
```

Export human-reviewed labels for future training:

```powershell
.\.venv\Scripts\python.exe -m src.export_verified_training_data --output data/verified_shadow_labels.csv
```

---

## Run tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -v
```

Run a specific test file:

```powershell
pytest tests/test_holdings_engine.py -v
```

With coverage:

```powershell
pytest tests/ -v --cov=src --cov-report=term-missing
```

---

## Replay output reports

After replay, these files are created in `reports/`:

| File | Contents |
|------|----------|
| `replay_dry_run_summary.json` | Counts summary (dry-run) |
| `replay_dry_run_final_holdings.csv` | Simulated holdings (dry-run) |
| `replay_dry_run_review_queue.csv` | Simulated review queue (dry-run) |
| `final_holdings.csv` | Applied holdings (safe-apply) |
| `manual_review_queue.csv` | Review queue (safe-apply) |
| `trade_event_ledger.csv` | All applied events |
| `dataset_parser_disagreements.csv` | Where parser ≠ dataset label |
| `replay_errors.csv` | Processing failures |

> **Holdings note**: Final holdings reflect positions reconstructable from the supplied historical period. They may not represent the complete real portfolio if the dataset does not start from a verified empty portfolio state.

---

## SQLite database location

```
storage/trade_holdings.db
```

Tables:

| Table | Purpose |
|-------|---------|
| `positions` | Latest state of each position |
| `trade_events` | Immutable event ledger |
| `processed_messages` | Idempotency log |
| `manual_review_queue` | Pending review items |
| `position_snapshots` | Holdings snapshot after each event |
| `schema_migrations` | Applied migration log |
| `incoming_messages` | Shadow-review input messages |
| `parser_predictions` | Immutable parser predictions for review |
| `human_reviews` | Human decisions and correction metadata |
| `shadow_events` | Events applied to the shadow portfolio |
| `verified_events` | Events approved into the verified portfolio |
| `verified_labels` | Human-reviewed labels for training export |

All Decimal values (allocations, prices, stop losses) are stored as `TEXT` for exact precision.

SQLite is intended here for local, limited single-writer operation. Do not treat
the Streamlit review app plus batch tools as a high-concurrency production
database deployment.

### Position identity

Every position is identified by the normalized full key:

```
portfolio_id + market_group + symbol + contract_month + option_type + strike_price + direction
```

Optional identity fields are persisted as non-null canonical strings:

- `contract_month = ""`
- `option_type = ""`
- `strike_price = ""`
- unresolved `market_group = "UNKNOWN"`

Repository lookups perform exact full-key matching when the event supplies the
distinguishing identity fields. Legacy or incomplete messages use guarded
fallback: all open positions in the same portfolio for the canonical symbol are
filtered by every supplied field, and the event may proceed only if exactly one
candidate remains. Ambiguous matches route to review with no position change.

Phase 2 migration `002_position_identity_indexes` normalizes identity columns,
audits duplicate full keys, and creates full-key indexes only when safe. It
never merges or deletes rows automatically.

---

## Core business rule

Entry percentages are customer buying-capacity units:

```
BUY 50% TSLA  -> add 50 LONG capacity units
SELL 50% TSLA -> add 50 SHORT capacity units
```

Repeated same-side entries add exposure and may exceed 100:

```
BUY 50% TSLA -> LONG 50
BUY 50% TSLA -> LONG 100
BUY 50% TSLA -> LONG 150
```

Standalone opposite-side instructions require context. Existing `LONG` + standalone
`SELL`, or existing `SHORT` + standalone `BUY`, is routed to review unless an
explicit close/reduction clause precedes it.

Ordered reversal messages are supported when a parent message contains exactly
two actionable clauses separated by `&`, semicolon, or a newline:

```
FULL PROFIT BOOK IN TSLA @1124 & 50% SELL TSLA @1124 SL 1200 TGT 1100-1050
```

The parser emits a parent `ParsedMessageBundle` with deterministic child IDs
`parent#1` and `parent#2`. Child 1 must be an explicit complete close, and child
2 must be the opposite-side entry for the same full instrument identity. Both
children are applied atomically; if either child fails, neither position change
is committed. Shadow application and verified approval both use this same
all-or-nothing ordered-event service.

All reduction percentages apply to the **current holding**, not the original:

```
new_holding = current_holding × (1 − reduction_percentage / 100)
```

Example chain (must be exact with Decimal arithmetic):

```
Start: 100%
50% profit book:  50% remains
50% profit book:  25% remains
50% profit book:  12.5% remains
50% profit book:  6.25% remains
```

Part profit = 25% of current holding.
Full profit / Exit / SL Touch = close entire remaining holding.

---

## Migration to PostgreSQL or MongoDB

The holdings engine depends only on the `Protocol` interfaces in `src/repository_interfaces.py`. To migrate:

1. Create `src/postgres_repository.py` implementing the same interfaces.
2. Wire the engine with the new repositories.
3. No changes required to `holdings_engine.py`, `hybrid_parser.py`, or any business logic.

---

## Current limitations

1. **Weak labels**: Training labels are rule-assisted, not fully human-verified. Model metrics represent initial baseline performance.
2. **No prior-event linking**: Corrections and IGNORE messages are sent to review. Automatic supersession requires additional implementation.
3. **No real-time feed**: The system processes messages in batch from CSV files.
4. **Missing portfolio start**: The production dataset may not start from an empty portfolio. Reduction/close messages without a known open position go to manual review.
5. **Group actions**: `CLOSE_GROUP` is classified and reviewed but not automatically applied.
6. **Multi-clause reversals**: Only explicit complete-close then opposite-entry ordered reversals are automatic. Partial-profit reversals, more than two actionable clauses, and simultaneous independent long/short cases still require review.
7. **Dataset/model follow-up**: Raw CSV candidate recovery, dataset relabelling, model retraining, historical replay regeneration, operational database migration, and fresh historical/shadow acceptance are not part of Phase 3.
8. **SQLite concurrency**: Local SQLite is suitable for controlled single-writer shadow review, not high-concurrency multi-user production review.

---

## Safety warnings

- **Never auto-apply corrections**: They require context linking to prior events.
- **Never auto-apply conditional instructions**: They require external condition confirmation.
- **Never treat standalone opposite-side entries as closes or reductions**: Existing `LONG` + `SELL`, or existing `SHORT` + `BUY`, requires context unless explicit close/reduction language is present.
- **Never allow ML predictions to bypass validation**: Confidence < 0.95 does not auto-apply.
- **Never process the same message_id twice**: The idempotency layer prevents duplicate application.
- **Never reset the database without `--confirm-reset`**: The flag is mandatory.
