# Current State

Last updated after Phase 4 reversal candidate recovery scanner.

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
- Phase 2 full instrument identity normalisation, exact lookup, guarded fallback, duplicate auditing, and SQLite identity indexes.
- Phase 3 atomic ordered reversal handling for explicit complete-close followed by opposite-side entry messages.
- Phase 4 raw CSV candidate recovery scanner that creates local human-review candidates only.

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

Position identity is the normalized full key:

- `portfolio_id`
- `market_group`
- `symbol`
- `contract_month`
- `option_type`
- `strike_price`
- `direction`

Optional identity fields are persisted as canonical non-null strings: blank contract month, option type, and strike for non-derivatives; `UNKNOWN` for unresolved market group. SQLite lookups use exact full identity when supplied. Incomplete legacy messages use guarded fallback only when filtering open positions by every supplied identity field leaves exactly one candidate. Multiple candidates route to review with `AMBIGUOUS_POSITION_IDENTITY`; no position is changed.

Shadow migration: `001_shadow_testing_tables`.

Shadow tables:

- `schema_migrations`
- `incoming_messages`
- `parser_predictions`
- `human_reviews`
- `shadow_events`
- `verified_events`
- `verified_labels`

Phase 2 migration: `002_position_identity_indexes`.

- Normalizes position identity fields in the target database before indexing.
- Audits duplicate normalized full keys before creating the unique full-key index.
- Refuses to merge/delete rows or create the unique index if duplicates exist.
- Was verified only against temporary test databases during Phase 2 implementation.

Phase 3 ordered reversal storage:

- Parent parser predictions store a `ParsedMessageBundle` JSON payload.
- Child events are persisted as individual immutable event rows.
- Child IDs are deterministic: `{parent_source_message_id}#1` and `{parent_source_message_id}#2`.
- Each child preserves `parent_source_message_id`, `child_event_index`, and dedicated `clause_text`.
- Shadow/verified event tables receive nullable child metadata columns when the shadow migration runs on temporary/test databases.
- No operational database migration was run as part of Phase 3.

## Phase 3 Ordered Reversals

An ordered reversal is one logical operation containing exactly two actionable clauses:

1. Explicit complete close of the current side.
2. Opposite-side entry for the same full instrument identity.

Supported close clauses are `FULL PROFIT BOOK`, `BOOK FULL PROFIT`, `EXIT FROM X`, `SL TOUCH IN X`, `SL TOUCHED IN X`, and `STOP LOSS HIT IN X`. `PART PROFIT` and percentage profit booking are not sufficient for an automatic reversal.

Supported entry clauses are `BUY X%`, `SELL X%`, plain stock `BUY`, and plain stock `SELL`. Entry percentages remain customer-buying-capacity units, plain stock entries default to 100 units, and cumulative exposure may exceed 100.

Clause splitting is deliberately conservative and uses only `&`, semicolon, actual newline, and escaped newline from source text. It does not split on hyphens, target ranges, slash-separated values, commas, decimal points, or ordinary `AND` prose. More than two actionable clauses route the parent to review.

Atomicity:

- Ordered children are applied through `src/ordered_event_service.py`.
- Both children execute inside one SQLite savepoint/transaction scope.
- If child 1 fails, child 2 is not executed.
- If child 2 fails, child 1 is rolled back.
- There is no Phase 3 partial-resume behavior.
- A detected half-committed legacy/corrupt sequence routes to `PARTIAL_ORDERED_SEQUENCE_DETECTED`.

Review reasons include `ORDERED_SPLIT_AMBIGUOUS`, `ORDERED_TOO_MANY_ACTIONABLE_CLAUSES`, `ORDERED_CHILD_1_NOT_FULL_CLOSE`, `ORDERED_CHILD_1_NO_POSITION`, `ORDERED_CHILD_1_AMBIGUOUS_IDENTITY`, `ORDERED_CHILD_2_INVALID_ENTRY`, `ORDERED_CHILD_IDENTITY_MISMATCH`, `ORDERED_NOT_OPPOSITE_DIRECTION`, `ORDERED_EXISTING_OPPOSITE_POSITION`, `ORDERED_CHILD_1_FAILED`, `ORDERED_CHILD_2_FAILED`, and `PARTIAL_ORDERED_SEQUENCE_DETECTED`.

Simultaneous independent long and short support remains deferred. If an opposite-side position is already open, the ordered message routes to review.

Portfolios are separated by `positions.portfolio_id`:

- `default` or historical: replay and normal local application
- `shadow`: parser-predicted shadow holdings
- `verified`: human-approved holdings

SQLite is suitable for this controlled local workflow, but it remains a limited single-writer database and is not a high-concurrency production review backend.

## Phase 4 Candidate Recovery

Phase 4 adds a read-only scanner for the immutable source file:

```text
data/raw/broadcast_admin.broadcasts.csv
```

The scanner fails closed unless the source has exactly `1704` data rows and SHA-256:

```text
824d76f16474c4dd08cf72e9382462169621510a4c861ed3bd941755f3ffad65
```

Local generated outputs:

- `data/review/phase4_reversal_candidates.csv`
- `data/review/phase4_reversal_candidates_manifest.json`
- `reports/phase4_reversal_candidate_summary.json`
- `reports/phase4_reversal_candidate_reason_counts.csv`
- `reports/phase4_reversal_parser_comparison.csv`
- `reports/phase4_reversal_source_integrity.json`

The candidate CSV and manifest contain proprietary source text and are ignored by Git. Aggregate reports contain counts, IDs, and parser comparison metadata, not full raw message text.

Every generated candidate starts with:

- `review_status = PENDING_REVIEW`
- `use_for_training = false`

No Phase 4 candidate is a trusted label. Holdings context is not reconstructed during the raw scan, so context validation is explicitly marked as not run. The scanner records structural parser evidence and Phase 3 bundle-parser comparison fields for human review.

## Phase 4 Candidate Human Review

The local human-review workflow is implemented without modifying scanner output.

Immutable scanner output:

- `data/review/phase4_reversal_candidates.csv`

Mutable local review decisions:

- `data/review/phase4_reversal_review_decisions.csv`

Regenerated merged export:

- `data/review/phase4_reversal_candidates_reviewed.csv`

Review decisions are keyed by `candidate_id`, written with a lock file and
atomic replacement, and include reviewer, reviewed timestamp, status,
review-layer bundle type, previous status, decision revision, review notes,
corrected fields, and `changed_fields_json`. The original scanner candidate
fields and parser output remain unchanged; corrections are stored separately.
Saving a decision is an upsert by `candidate_id`; repeated saves and Streamlit
reruns do not duplicate decision rows. `Clear Decision` removes the decision row
and returns the candidate to `PENDING_REVIEW`.

Authoritative text-level review status:

- `CONFIRMED_ORDERED_CLOSE_THEN_ENTRY`

Authoritative review-layer bundle type:

- `ORDERED_CLOSE_THEN_ENTRY`

This means:

- clause 1 fully closes the current database position
- clause 2 opens a new `BUY` or `SELL` entry
- both clauses concern the same full non-direction instrument identity
- clause order is clear

The old `CONFIRMED_ORDERED_REVERSAL` status remains readable for backward
compatibility and is marked deprecated in the UI. Existing decisions are not
silently rewritten.

Portfolio-context outcome is stored separately:

- `portfolio_effect_status = NOT_EVALUATED`
- `REVERSAL`
- `SAME_SIDE_REENTRY`
- `NO_OPEN_POSITION`
- `AMBIGUOUS_POSITION`
- `IDENTITY_CONFLICT`

Raw Phase 4 historical candidate review defaults to `NOT_EVALUATED` unless a
trustworthy historical database snapshot is explicitly available. Do not infer
`REVERSAL` from text alone.

Training flags are split:

- `use_for_structure_training`: text split, full-close clause, new entry clause,
  non-direction identity, and parsed labels are reliable.
- `use_for_portfolio_effect_training`: prior database position side is known
  from trustworthy context and the outcome is labelled `REVERSAL` or
  `SAME_SIDE_REENTRY`.
- legacy `use_for_training`: readable for backward compatibility and mapped to
  structure training when old rows do not contain the new fields; it does not
  mean portfolio-effect training.

Evaluation metrics must also be split:

- Text-structure metrics: ordered close-then-entry detection precision/recall/F1,
  clause split accuracy, full-close instruction accuracy, `BUY/SELL` entry
  accuracy, full non-direction identity accuracy, entry percentage accuracy, and
  price/stop-loss/target extraction accuracy.
- Portfolio-context metrics: `REVERSAL` accuracy, `SAME_SIDE_REENTRY` accuracy,
  `NO_OPEN_POSITION` handling, `AMBIGUOUS_POSITION` handling, exact position
  identity resolution, and atomic sequence success/rollback.

Candidates with `portfolio_effect_status = NOT_EVALUATED` are excluded from
portfolio-effect metric denominators. `ORDERED_CHILD_1_NO_POSITION` from the
offline scanner does not count as a text-structure parsing error.

Start the review app:

```powershell
.\.venv\Scripts\python.exe -m streamlit run app/phase4_candidate_review_app.py
```

Generate review summaries:

```powershell
.\.venv\Scripts\python.exe -m src.phase4_review_summary
```

Generated local reports:

- `reports/phase4_human_review_summary.json`
- `reports/phase4_human_review_status_counts.csv`
- `reports/phase4_human_review_training_eligibility.csv`
- `reports/phase4_human_review_progress.csv`

False positives, ambiguous or needs-context records, gibberish, and do-not-use
records cannot be marked training eligible. Phase 5 relabelling and model
retraining remain blocked until human review is complete and explicitly
approved.

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

Current focused verification:

- Identity tests: `14 passed`
- SQLite repository tests: `9 passed`
- Holdings-engine tests: `18 passed`
- Shadow-workflow tests: `16 passed`, `6 warnings`

Full suite after Phase 4 human-review workflow semantics correction:

- Collected: `215`
- Passed: `215`
- Failed: `0`
- Skipped: `0`
- Warnings: `6`

Warnings are NumPy/joblib deprecation warnings during model unpickling, not scikit-learn version mismatch warnings.

Phase 4 focused tests:

- `18 passed`
- `6` NumPy/joblib deprecation warnings during model unpickling

Phase 4 human-review workflow focused tests:

- `18 passed`

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
- Raw CSV candidate recovery, dataset relabelling, model retraining, historical replay regeneration, operational database migration, and fresh historical/shadow acceptance remain deferred to Phase 4 or later.
- SQLite is local and single-writer oriented.

## Next Planned Task

Human review of Phase 4 candidates remains pending. Dataset relabelling, model retraining, historical replay regeneration, operational database migration, shadow acceptance testing, and Phase 5 remain blocked until human review is complete and explicitly approved.
