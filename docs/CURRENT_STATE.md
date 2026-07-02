# Current State

Last updated after Phase 1 v2 trade-semantics implementation.

## Git

- Branch: `codex-shadow-acceptance`
- Commit before stabilization commit: `e0e0c12`

## Implemented Components

- Hybrid trade-message parser: text normalization, aliases, entity extraction, deterministic rules, scikit-learn classifier, hybrid resolver, and validation.
- Deterministic holdings engine using Decimal arithmetic and repository interfaces.
- SQLite persistence for positions, trade events, processed messages, manual review queue, and snapshots.
- Historical replay with dry-run and safe-apply modes.
- Shadow-review workflow with separate `shadow` and `verified` portfolios.
- Streamlit human-review app.
- Shadow ingestion CLIs, verified-position import, verified-label export, and shadow metrics.
- Audit and report scripts under `scripts/`.
- Phase 1 v2 semantics for surface instructions, entry capacity units, current-position reductions, and opposite-side conflict handling.

## Phase 1 Semantics

Newly parsed events use `semantics_version = "v2"` and may include:

- `surface_instruction`
- `entry_capacity_pct`
- `reduction_pct`
- `position_effect`
- `resolved_position_side`

Entry and same-side addition percentages are customer buying-capacity units:

- `BUY 50% TSLA` opens or increases `LONG` exposure by `50`.
- `SELL 50% TSLA` opens or increases `SHORT` exposure by `50`.
- Repeated same-side entries add capacity units and may exceed `100`.
- Individual stock entries with no explicit percentage default to `100` capacity units when the symbol is confidently extracted.

Reduction percentages apply to the current position:

- `PART PROFIT` reduces the current position by `25%`.
- `50% PROFIT BOOK` reduces the current position by `50%`.
- Repeated reductions compound on the remaining exposure.

Standalone opposite-side entries require context:

- Existing `LONG` + standalone `SELL` is `OPPOSITE_DIRECTION_CONFLICT` / review.
- Existing `SHORT` + standalone `BUY` is `OPPOSITE_DIRECTION_CONFLICT` / review.
- Ordered reversal handling remains deferred to Phase 3.

## Database Architecture

Primary database: `storage/trade_holdings.db`.

Base tables:

- `positions`
- `trade_events`
- `processed_messages`
- `manual_review_queue`
- `position_snapshots`

Shadow migration: `001_shadow_testing_tables`.

Shadow tables:

- `schema_migrations`
- `incoming_messages`
- `parser_predictions`
- `human_reviews`
- `shadow_events`
- `verified_events`
- `verified_labels`

Portfolios are separated by `positions.portfolio_id`:

- `default` or historical: replay and normal local application
- `shadow`: parser-predicted shadow holdings
- `verified`: human-approved holdings

SQLite is suitable for this controlled local workflow, but it remains a limited single-writer database and is not a high-concurrency production review backend.

## Model

- Local project interpreter: `C:\Astrodunia text parsing\trade_message_system\.venv\Scripts\python.exe`
- Python: `3.13.9`
- scikit-learn: `1.9.0`
- Model metadata already records Python `3.13.9` and scikit-learn `1.9.0`.
- `requirements.txt` and `pyproject.toml` pin `scikit-learn==1.9.0` to match the saved local model.

Pilot test metrics from `reports/model_audit_summary.json`:

- Accuracy: `0.8222`
- Macro F1: `0.5418`
- Weighted F1: `0.84`

Labels are weak/rule-assisted and not a fully human-verified gold standard.

## Historical Audit Result

From `reports/final_summary.json`:

- Total source messages: `1043`
- Parsed: `1043`
- Applied: `505`
- Manual review: `490`
- Status-only: `43`
- Non-trade skipped: `5`
- Failed: `0`
- Parser/dataset disagreements: `49`

Historical holdings represent only positions reconstructable from the supplied period.

## Shadow Workflow Status

Shadow workflow code and tests are present. The stabilization pass fixed literal `REDUCE X%` messages so `REDUCE 50% IN RELIANCE` now resolves to:

- `final_action = REDUCE_POSITION`
- `symbol = RELIANCE`
- `quantity_percent = 50`
- `quantity_basis = CURRENT_POSITION`

End-to-end shadow acceptance testing has not started yet.

## Test Result

Command:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -v --tb=short
```

Current Phase 1 focused verification:

- Parser tests: `78 passed`
- Holdings-engine tests: `18 passed`
- Shadow-workflow tests: `15 passed`, `6 warnings`

Full suite:

- Collected: `148`
- Passed: `148`
- Failed: `0`
- Skipped: `0`
- Warnings: `6`

Warnings are NumPy/joblib deprecation warnings during model unpickling, not scikit-learn version mismatch warnings.

## Commands

Install dependencies:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Run full test suite:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -v --tb=short
```

Start Streamlit shadow-review app:

```powershell
.\.venv\Scripts\python.exe -m streamlit run app/shadow_review_app.py
```

Export verified labels:

```powershell
.\.venv\Scripts\python.exe -m src.export_verified_training_data --output data/verified_shadow_labels.csv
```

## Databases Not To Reset

Do not reset, delete, or migrate these databases unless explicitly instructed:

- `storage/trade_holdings.db`
- `storage/trade_holdings_audit.db`

Use `storage/shadow_acceptance.db` for acceptance testing.

## Remaining Limitations

- Weak labels remain a baseline, not a gold-standard dataset.
- Corrections and ignore/cancel messages still require prior-event context.
- Real-time feed ingestion is not implemented.
- Production data may not begin from a verified empty portfolio.
- Group actions are reviewed but not automatically applied.
- Position identity remains a known Phase 2 limitation: repository lookups still primarily use portfolio, symbol, and direction; full market/contract/option identity enforcement is not implemented yet.
- Option-contract keying remains simplified until Phase 2.
- SQLite is local and single-writer oriented.

## Next Planned Task

Phase 2: enforce full instrument position identity and guarded unique-candidate fallback.
