# Trade Message System

## 1. Project Overview

This repository contains the Python parser, validation, shadow-testing, and holdings engine for trade/broadcast messages.

It turns raw messages such as entry calls, profit-booking updates, exits, and ordered close-then-entry instructions into structured trade events. Those events are validated, applied to shadow or verified holdings only when safe, and captured for human review, labels, and reports.

The current implementation is a standalone local Python workflow using SQLite and Streamlit. The intended product architecture is larger:

- Node.js backend receives live/broadcast messages and owns production orchestration.
- React frontend presents review queues, diffs, and approval workflows.
- Python parser/engine service preserves deterministic trade semantics and safe holdings application.
- Human-reviewed portfolio state becomes the trusted source for future parsing, reporting, and model improvement.

## 2. High-Level Architecture

```text
raw message source
  -> Python ingest CLI/service
  -> parser layer
     -> rule parser
     -> ML/hybrid parser
     -> entity extractor
  -> validator
  -> shadow service
  -> holdings engine / ordered event service
  -> SQLite repository
  -> Streamlit review apps
  -> human review decisions
  -> verified holdings, labels, reports
```

Key modules:

| Layer | Files | Purpose |
|---|---|---|
| Parser layer | `src/hybrid_parser.py`, `src/rule_parser.py` | Resolve deterministic rules, ML predictions, and ordered bundles into structured events. |
| Entity extraction | `src/entity_extractor.py` | Extract symbols, prices, targets, stop loss, market hints, and multi-instrument signals. |
| Validation | `src/validator.py` | Blocks invalid, ambiguous, unsafe, and unsupported single-event predictions before approval/application. |
| Holdings engine | `src/holdings_engine.py` | Applies validated events with Decimal arithmetic and full instrument identity. |
| Ordered event service | `src/ordered_event_service.py` | Applies ordered close-then-entry bundles atomically. |
| Shadow service | `src/shadow_service.py` | Ingests predictions, applies shadow state, records human reviews, and updates verified state only after approval. |
| Position identity | `src/position_identity.py` | Canonicalizes and resolves full instrument keys. |
| SQLite repository | `src/database.py`, repository implementations | Local persistence for positions, event ledgers, predictions, reviews, labels, and migrations. |
| CLI tools | `src/ingest_shadow_message.py`, `src/import_verified_positions.py`, `scripts/migrate_shadow_tables.py` | Local setup, migration, seeding, and message ingestion. |
| Review apps | `app/shadow_review_app.py`, `app/phase4_candidate_review_app.py` | Streamlit review workflows for live/shadow review and historical Phase 4 candidate review. |

Future Node/React integration should move orchestration, authentication, queue ownership, and production storage into the main app stack while preserving the Python parser and engine safety rules.

## 3. Message Processing Flow

```text
raw message
  -> ingest
  -> parse
  -> validate
  -> shadow apply if safe
  -> human review
  -> approve / reject / edit
  -> verified holdings update
  -> labels / reports
```

Important boundary: verified holdings should not change before human approval. Shadow holdings may change for safe predictions so reviewers can compare parser intent against the verified portfolio.

## 4. Current Status / What Has Been Completed

- Phase 1: v2 entry/reduction semantics implemented.
- Phase 2: full instrument identity implemented and indexed.
- Phase 3: atomic ordered close-then-entry handling implemented.
- Phase 4: raw candidate recovery and human review workflow implemented.
- Phase 4 correction: text-level close-then-entry labels are separated from runtime portfolio outcomes.
- Phase 5: reviewed label export and parser evaluation implemented.
- Phase 5.1: reviewed parser regressions fixed.
- Phase 6: acceptance passed with verdict `READY_FOR_CONTROLLED_MANUAL_SHADOW_PILOT`.
- Pilot v3 smoke test passed with verdict `READY_FOR_SMALL_LIVE_SHADOW_PILOT`.
- Live pilot v1 found an invalid approval safety issue: invalid parser predictions could be approved as parsed.
- Safety fix added: invalid approvals are blocked.
- Safety fix added: unsupported multi-instrument single-event approvals are blocked.

Latest local tags include:

- `phase-1-v2-semantics`
- `phase-2-position-identity`
- `phase-3-ordered-reversals`
- `phase-4-review-workflow`
- `phase-5-reviewed-label-export`
- `phase-5-1-parser-regressions-fixed`
- `phase-6-ready-for-shadow-pilot`
- `pilot-v3-smoke-test-pass`
- `invalid-approval-safety-fix`
- `multi-instrument-single-event-safety-fix`

## 5. What Is Left / Roadmap

- Continue controlled live pilot v2.
- Generate a live pilot v2 report.
- Integrate with the Node.js backend.
- Build a React review UI or integrate the current review workflow into the product frontend.
- Decide API contracts for ingest, parse, review, approve, reject, edit-approve, holdings, and audit events.
- Replace manual CLI ingestion with backend-owned live/broadcast ingestion.
- Build auth, user, and portfolio mapping.
- Add a production database strategy.
- Add monitoring and audit logs.
- Decide a model retraining schedule.
- Collect more human-reviewed labels.
- Improve multi-instrument splitting into multiple child events.
- Improve segment/market normalization UX.
- Add deployment packaging.
- Add CI/CD.
- Decide when, if ever, auto-apply is allowed. Currently manual approval remains required.

## 6. Setup Instructions

Use the project virtual environment:

```powershell
cd "C:\Astrodunia text parsing\trade_message_system"
.\.venv\Scripts\python.exe --version
```

Install dependencies only if the environment is missing packages:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Set an isolated database path in every terminal:

```powershell
$env:TRADE_DB_PATH = "C:\Astrodunia text parsing\trade_message_system\storage\shadow_live_pilot_v2.db"
```

Initialize the database:

```powershell
.\.venv\Scripts\python.exe -m src.database
```

Run shadow-table migrations:

```powershell
.\.venv\Scripts\python.exe scripts\migrate_shadow_tables.py --db $env:TRADE_DB_PATH
```

Import verified positions:

```powershell
.\.venv\Scripts\python.exe -m src.import_verified_positions --input data\verified_initial_positions_live_pilot_v2.csv --db $env:TRADE_DB_PATH --dry-run
.\.venv\Scripts\python.exe -m src.import_verified_positions --input data\verified_initial_positions_live_pilot_v2.csv --db $env:TRADE_DB_PATH --apply --confirm
```

Run tests when you need verification:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -v --tb=short --basetemp .pytest_tmp_full
```

Launch the local review app:

```powershell
.\.venv\Scripts\python.exe -m streamlit run app/shadow_review_app.py
```

Do not start Streamlit or live ingestion unless that is the explicit task.

## 7. Environment Variables

### `TRADE_DB_PATH`

`TRADE_DB_PATH` controls which SQLite file the Python code uses through `src.config.DATABASE_PATH`.

Set it in every PowerShell terminal before initializing, migrating, importing, ingesting, reviewing, or reporting:

```powershell
$env:TRADE_DB_PATH = "C:\Astrodunia text parsing\trade_message_system\storage\shadow_live_pilot_v2.db"
.\.venv\Scripts\python.exe -c "from src.config import DATABASE_PATH; print(DATABASE_PATH)"
```

If a terminal does not set `TRADE_DB_PATH`, tools may fall back to the default database path. That can accidentally write pilot data to the wrong SQLite database.

## 8. Database Safety Rules

- Do not use operational databases for tests.
- Do not use `storage/trade_holdings.db` for pilots.
- Use fresh pilot databases, for example `storage/shadow_live_pilot_v2.db`.
- Do not reuse issue-discovery databases as final evidence.
- Keep pilot databases archived for auditability.
- Do not reset, migrate, or mutate historical/operational databases unless the task explicitly approves that exact action.
- `storage/shadow_manual.db` is pre-refactor and should not be used as trusted labels.

## 9. CLI Usage

Initialize the configured database:

```powershell
$env:TRADE_DB_PATH = "C:\Astrodunia text parsing\trade_message_system\storage\shadow_live_pilot_v2.db"
.\.venv\Scripts\python.exe -m src.database
```

Run migrations:

```powershell
.\.venv\Scripts\python.exe scripts\migrate_shadow_tables.py --db $env:TRADE_DB_PATH
```

Import verified positions:

```powershell
.\.venv\Scripts\python.exe -m src.import_verified_positions --input data\verified_initial_positions_live_pilot_v2.csv --db $env:TRADE_DB_PATH --dry-run
.\.venv\Scripts\python.exe -m src.import_verified_positions --input data\verified_initial_positions_live_pilot_v2.csv --db $env:TRADE_DB_PATH --apply --confirm
```

Ingest one shadow message:

```powershell
.\.venv\Scripts\python.exe -m src.ingest_shadow_message --message-id "pilot-v2-001" --text "BUY 50% TSLA" --segment "GLOBAL_EQUITY"
```

Verify the active database path:

```powershell
.\.venv\Scripts\python.exe -c "from src.config import DATABASE_PATH; print(DATABASE_PATH)"
```

Run tests:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -v --tb=short --basetemp .pytest_tmp_full
```

## 10. Verified Positions CSV Format

Required columns:

```text
symbol,direction,current_allocation_pct,market_group,contract_month,option_type,strike_price,average_entry_price,stop_loss,targets,status,as_of_timestamp
```

Example rows:

```csv
symbol,direction,current_allocation_pct,market_group,contract_month,option_type,strike_price,average_entry_price,stop_loss,targets,status,as_of_timestamp
SBIN,LONG,100,NSE,,,,800,780,"820,850",OPEN,2026-07-09T00:00:00+05:30
GOLD,LONG,50,MCX,AUG,,,74000,73500,"74500,75000",OPEN,2026-07-09T00:00:00+05:30
AAPL,LONG,100,GLOBAL_EQUITY,,,,200,190,"210,220",OPEN,2026-07-09T00:00:00+05:30
GOLD,LONG,100,International Market,AUG,,,4102,4000,"4500,4600",OPEN,2026-07-09T00:00:00+05:30
NIFTY,SHORT,75,Indian Indices,JULY,,,25000,25200,"24800,24600",OPEN,2026-07-09T00:00:00+05:30
```

Keep the CSV local unless it is intentionally sanitized and approved for commit.

## 11. Segment / Market Group Guidance

Examples:

| Market group | Symbols |
|---|---|
| `GLOBAL_EQUITY` | `AAPL`, `TSLA`, `NVDA`, `MSFT` |
| `NSE` | `SBIN`, `HDFCBANK`, `INFY`, `RELIANCE`, `NIFTY`, `BANKNIFTY` |
| `MCX` | `GOLD`, `SILVER`, `COPPER`, `CRUDE_MINI`, `CRUDEOILM` |
| `International Market` | `GOLD AUG`, `SILVER JULY`, `SP500`, `NASDAQ`, `DOW`, `RUSSELL` |
| `Indian Indices` | `NIFTY`, `BANKNIFTY`, `FINNIFTY`, `SENSEX` |

Segment values must match seeded position identity exactly. For example, `GLOBAL_EQUITY` and `Global Equity` are not interchangeable for full-key position matching.

## 12. Streamlit App Usage

Main app:

```powershell
.\.venv\Scripts\python.exe -m streamlit run app/shadow_review_app.py
```

Use it to inspect the review queue and choose:

- Approve as parsed.
- Edit and approve.
- Reject.
- Needs context.
- Duplicate.
- Non-trade.

Safety behavior:

- Invalid predictions cannot be approved as parsed.
- Unsupported multi-instrument single-event predictions cannot be approved as parsed.
- Edit-and-approve must provide a corrected valid event or bundle.
- Verified portfolio should not change before human approval.
- Parser predictions are immutable; corrections and decisions are stored separately.

## 13. Phase 4 Candidate Review App

Historical candidate review app:

```powershell
.\.venv\Scripts\python.exe -m streamlit run app/phase4_candidate_review_app.py
```

This app is for recovered historical Phase 4 close-then-entry candidates. It is not needed for normal live pilot work unless you are reviewing those recovered historical candidates.

## 14. Testing

Full suite:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -v --tb=short --basetemp .pytest_tmp_full
```

Latest known documented full-suite result from `docs/CURRENT_STATE.md` after Phase 5 was `228 passed`. Phase 6 and later safety fixes added more code and commits; rerun the suite before relying on a current count.

Focused categories:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_holdings_engine.py -v --tb=short
.\.venv\Scripts\python.exe -m pytest tests/test_shadow_workflow.py -v --tb=short
.\.venv\Scripts\python.exe -m pytest tests/test_sqlite_repository.py -v --tb=short
.\.venv\Scripts\python.exe -m pytest tests/test_phase4_review_workflow.py -v --tb=short
```

## 15. Model / ML Notes

- Model artifacts exist under `models/`, but deterministic rules and validation are critical.
- ML confidence is often below any safe unattended threshold.
- Manual approval remains required.
- Do not overwrite model artifacts during pilots.
- Phase 5 exported reviewed labels for close-then-entry structure, but retraining was intentionally not done yet.
- Reviewed labels and model outputs may contain sensitive or proprietary message text; treat them as local artifacts unless explicitly approved.

## 16. Data Privacy / Git Safety

Raw CSVs, databases, reviewed labels, derived files, reports, and model artifacts should generally not be committed.

Before any push or commit, inspect status:

```powershell
git status --short
```

Inspect tracked sensitive-looking files:

```powershell
git ls-files | Select-String "storage|data/raw|data/review|data/derived|reports|models|\.db|\.csv|\.zip"
```

Local `.gitignore` rules should protect datasets and databases, but always verify. Pilot DBs, report CSVs, raw exports, and reviewed-label files can contain proprietary source text.

## 17. Node.js / React Integration Plan

Expected future architecture:

1. Node.js backend receives broadcast/live messages.
2. Backend sends message text, `source_message_id`, segment, user, portfolio, and trusted position context to the Python parser service.
3. Python returns a structured prediction, validation status, review reason, and any ordered child events.
4. Backend stores the prediction and review state.
5. React frontend displays the review queue, parser output, warnings, and shadow/verified portfolio diffs.
6. Human reviewer approves, rejects, marks needs-context/duplicate, or edits and approves.
7. Backend calls an approval/apply service.
8. Verified holdings update only after approval.
9. Backend exposes audit events for compliance and debugging.

Future API endpoints should include:

- `POST /parse-message`
- `POST /ingest-message`
- `GET /review-queue`
- `POST /review/:id/approve`
- `POST /review/:id/reject`
- `POST /review/:id/edit-approve`
- `GET /holdings/verified`
- `GET /holdings/shadow`
- `GET /audit/events`

The Python app currently uses SQLite and Streamlit for local validation. The product integration should move orchestration into Node/React while preserving parser/engine safety rules.

## 18. Recommended API Contract Draft

### Parse Message Request

```json
{
  "source_message_id": "broadcast-123",
  "raw_text": "BUY 50% TSLA @250 SL 240 TGT 270",
  "segment": "GLOBAL_EQUITY",
  "portfolio_id": "portfolio-001",
  "user_id": "user-001",
  "as_of_timestamp": "2026-07-09T10:00:00+05:30"
}
```

### Parse Message Response

```json
{
  "source_message_id": "broadcast-123",
  "prediction_type": "event",
  "validation_status": "VALID",
  "needs_review": false,
  "auto_apply_eligible": true,
  "event": {
    "final_action": "OPEN_LONG",
    "symbol": "TSLA",
    "market_group": "GLOBAL_EQUITY",
    "direction": "LONG",
    "quantity_percent": "50",
    "quantity_basis": "CUSTOMER_BUYING_CAPACITY",
    "position_effect": "OPEN"
  },
  "warnings": []
}
```

### Ordered Bundle Response

```json
{
  "source_message_id": "broadcast-124",
  "prediction_type": "bundle",
  "bundle_type": "ORDERED_CLOSE_THEN_ENTRY",
  "is_ordered": true,
  "validation_status": "VALID",
  "children": [
    {
      "source_message_id": "broadcast-124#1",
      "final_action": "CLOSE_POSITION",
      "symbol": "AAPL",
      "market_group": "GLOBAL_EQUITY",
      "direction": "LONG",
      "quantity_basis": "CURRENT_POSITION",
      "position_effect": "CLOSE"
    },
    {
      "source_message_id": "broadcast-124#2",
      "final_action": "OPEN_SHORT",
      "symbol": "AAPL",
      "market_group": "GLOBAL_EQUITY",
      "direction": "SHORT",
      "quantity_percent": "50",
      "quantity_basis": "CUSTOMER_BUYING_CAPACITY",
      "position_effect": "OPEN"
    }
  ],
  "portfolio_effect_status": "REVERSAL"
}
```

### Validation Invalid Response

```json
{
  "source_message_id": "broadcast-125",
  "prediction_type": "event",
  "validation_status": "INVALID",
  "needs_review": true,
  "auto_apply_eligible": false,
  "review_reason": "ENTRY_CAPACITY_MISSING",
  "event": {
    "final_action": "OPEN_LONG",
    "symbol": "GOLD",
    "market_group": "International Market",
    "direction": "LONG",
    "quantity_percent": null,
    "quantity_basis": "CUSTOMER_BUYING_CAPACITY"
  },
  "warnings": [
    "Entry capacity missing; cannot default non-stock international entry."
  ]
}
```

### Approval Request

```json
{
  "source_message_id": "broadcast-123",
  "reviewer_id": "reviewer-001",
  "decision": "APPROVE_AS_PARSED",
  "notes": "Matches verified position context."
}
```

### Holdings Diff Response

```json
{
  "source_message_id": "broadcast-123",
  "portfolio_id": "portfolio-001",
  "status": "APPROVED",
  "diff": [
    {
      "identity": {
        "portfolio_id": "portfolio-001",
        "market_group": "GLOBAL_EQUITY",
        "symbol": "TSLA",
        "contract_month": "",
        "option_type": "",
        "strike_price": "",
        "direction": "LONG"
      },
      "before": null,
      "after": {
        "current_allocation_pct": "50",
        "status": "OPEN"
      }
    }
  ]
}
```

## 19. Known Pitfalls

- Wrong `TRADE_DB_PATH` terminal.
- Wrong segment spelling or case.
- Approving invalid predictions.
- Multi-instrument messages collapsing into one event.
- Missing entry capacity for non-stock or international entries.
- Reusing old pilot databases.
- Treating `storage/shadow_manual.db` as trusted labels.
- Assuming `SELL` against an existing long means reduce or reverse.
- Assuming text alone can decide `REVERSAL` versus `SAME_SIDE_REENTRY`.

## 20. Quick Start: Local Pilot

```powershell
cd "C:\Astrodunia text parsing\trade_message_system"
$env:TRADE_DB_PATH = "C:\Astrodunia text parsing\trade_message_system\storage\shadow_live_pilot_v2.db"

.\.venv\Scripts\python.exe -c "from src.config import DATABASE_PATH; print(DATABASE_PATH)"
.\.venv\Scripts\python.exe -m src.database
.\.venv\Scripts\python.exe scripts\migrate_shadow_tables.py --db $env:TRADE_DB_PATH
.\.venv\Scripts\python.exe -m src.import_verified_positions --input data\verified_initial_positions_live_pilot_v2.csv --db $env:TRADE_DB_PATH --dry-run
.\.venv\Scripts\python.exe -m src.import_verified_positions --input data\verified_initial_positions_live_pilot_v2.csv --db $env:TRADE_DB_PATH --apply --confirm
.\.venv\Scripts\python.exe -m streamlit run app/shadow_review_app.py
.\.venv\Scripts\python.exe -m src.ingest_shadow_message --message-id "pilot-v2-001" --text "BUY 50% TSLA" --segment "GLOBAL_EQUITY"
```

Then review the message in Streamlit. Approve only if parser output and portfolio diff are correct.

## 21. Current Recommended Next Step

Continue a small controlled live pilot v2 after the safety fixes:

- Use a fresh database such as `storage/shadow_live_pilot_v2.db`.
- Process 10-20 messages.
- Require human review.
- Generate a live pilot v2 report.
- Do not start automated production ingestion yet.

## Business Semantics Reference

### BUY/SELL Entries

- `BUY 50%` adds 50 customer buying-capacity units to `LONG`.
- `SELL 50%` adds 50 customer buying-capacity units to `SHORT`.
- Repeated same-side entries can exceed 100: `50 -> 100 -> 150`.
- There is no artificial cap at 100.

### Reductions And Closes

- `PART PROFIT` reduces 25% of the current position.
- `50% PROFIT BOOK` reduces 50% of the current position.
- `FULL PROFIT BOOK`, `EXIT`, `SL TOUCH`, and `STOP LOSS HIT` close the current position.

### Standalone Opposite-Side Entries

- Existing `LONG` + standalone `SELL` routes to review/conflict.
- Existing `SHORT` + standalone `BUY` routes to review/conflict.
- The system must not auto-reduce or auto-reverse without an explicit close.

### Ordered Close-Then-Entry

Example:

```text
EXIT FROM X & 50% SELL X
```

- Clause 1 fully closes the current database position.
- Clause 2 opens a new `BUY` or `SELL` position.
- Previous `LONG` + new `SELL` = `REVERSAL`.
- Previous `SHORT` + new `BUY` = `REVERSAL`.
- Previous `LONG` + new `BUY` = `SAME_SIDE_REENTRY`.
- Previous `SHORT` + new `SELL` = `SAME_SIDE_REENTRY`.
- All four are valid.
- If the close clause has no matching open position, block the entire sequence.
- Child 2 must not execute if child 1 fails.
- If child 2 fails, child 1 must roll back.
- The sequence must be atomic.

### Full Instrument Identity

Every position must respect:

```text
portfolio_id
market_group
symbol
contract_month
option_type
strike_price
direction
```

Avoid symbol-only matching. Same symbols can exist across markets, contract months, option types, strikes, and directions.

### Invalid Predictions

- `INVALID` predictions cannot be approved as parsed.
- Missing entry capacity for unsafe categories must not silently default to 100.
- Unsupported multi-instrument single-event messages must route to review/split-required.
- Human approval is required for low-confidence, invalid, ambiguous, or context-dependent cases.
