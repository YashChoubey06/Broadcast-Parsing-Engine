# Shadow Acceptance Specification

Current stable baseline:

- Branch: `codex-shadow-acceptance`
- Commit: `b1c26ba`
- Python: `3.13.9`
- scikit-learn: `1.9.0`
- Current suite: `130` tests passing

This document defines the end-to-end acceptance requirements for the existing shadow-testing and human-review system. It is a specification only. Do not implement or run the acceptance test until explicitly instructed.

## 1. Safety and Isolation

- Use only `storage/shadow_acceptance.db` for acceptance testing.
- Never modify `storage/trade_holdings.db`.
- Never modify `storage/trade_holdings_audit.db`.
- Preserve `default` / historical, `shadow`, and `verified` portfolios separately.
- Shadow events must never automatically modify verified positions.
- The acceptance harness must fail fast if it is pointed at any database other than `storage/shadow_acceptance.db`.
- The original CSV datasets under `data/` must not be modified.

## 2. Migration Verification

Acceptance must:

- Run the shadow migration twice.
- Verify migration idempotency.
- Verify `schema_migrations` records each migration exactly once.
- Verify SQLite foreign keys are enabled for every acceptance connection.
- Verify no historical tables are dropped.
- Verify no historical tables are modified unexpectedly.
- Verify these base tables still exist after migration:
  - `positions`
  - `trade_events`
  - `processed_messages`
  - `manual_review_queue`
  - `position_snapshots`
- Verify these shadow tables exist after migration:
  - `incoming_messages`
  - `parser_predictions`
  - `human_reviews`
  - `shadow_events`
  - `verified_events`
  - `verified_labels`

## 3. Full Test-Suite Verification

Acceptance must:

- Run all tests using the project virtual environment:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -v --tb=short
```

- Stop on failures.
- Report:
  - collected tests
  - passed tests
  - failed tests
  - skipped tests
  - warnings
- Build a coverage matrix for all required shadow-workflow scenarios listed in this specification.
- Refuse to proceed to scenario execution if the full suite fails.

## 4. Verified Initial-Position Import

Create an acceptance CSV containing:

- verified `NIFTY` `LONG` at `100%`
- verified `NVDA` `LONG` at `75%`

Acceptance must:

- Test the import in dry-run mode first.
- Then apply with explicit confirmation.
- Verify the `shadow` portfolio remains empty after import.
- Verify repeated import cannot silently overwrite positions.
- Verify repeated import either returns a safe duplicate/blocked result or requires explicit overwrite confirmation.
- Verify imported positions are in the `verified` portfolio only.

## 5. Current-Holding Percentage Workflow

Seed state:

- verified `NIFTY` `LONG` at `100%`

Ingest:

- `acceptance-001`: `SELL 50% NIFTY`

Expected after ingestion:

- verified `NIFTY` remains `100%`
- shadow `NIFTY` becomes `50%`

Approve `acceptance-001`.

Expected after approval:

- verified `NIFTY` becomes `50%`
- shadow `NIFTY` remains `50%`

Ingest:

- `acceptance-002`: `SELL 50% NIFTY`

Expected after ingestion:

- verified `NIFTY` remains `50%`
- shadow `NIFTY` becomes `25%`

Approve `acceptance-002`.

Expected after approval:

- verified `NIFTY` becomes `25%`
- shadow `NIFTY` remains `25%`

This must prove reductions use:

```text
new = current * (1 - percentage / 100)
```

The acceptance report must show the before and after values for both portfolios at every step.

## 6. Approval-Time Revalidation

Acceptance must:

- Ingest a pending reduction.
- Change verified holdings through another approved event.
- Approve the original pending event.
- Confirm approval uses the latest verified position, not stale ingestion-time state.
- Record the stale ingestion-time expected value and the actual approval-time verified value in `reports/shadow_acceptance_revalidation.json`.

## 7. Edit and Approve

Ingest:

- `acceptance-003`: `PART PROFIT BOOK IN NVDA`

Expected initial parser proposal:

- `final_action = REDUCE_POSITION`
- `symbol = NVDA`
- `quantity_percent = 25`
- `quantity_basis = CURRENT_HOLDING`

Edit the event to a `50%` reduction and approve it.

Expected:

- verified `NVDA` changes from `75%` to `37.5%`
- original parser prediction remains immutable
- corrected event records `50%`
- decision is `APPROVED_WITH_CORRECTION`
- `changed_fields_json` includes `quantity_percent`
- shadow history retains the original parser behavior
- verified event uses the corrected event
- verified label records that the event was corrected

## 8. Rejection

Ingest:

- `acceptance-004`: `BUY 50% AMD @160`

Reject it.

Verify:

- verified `AMD` does not change
- shadow history remains available
- original prediction remains available
- no verified event is created
- the human review decision is recorded as rejected

## 9. Non-Trade and Needs-Context Decisions

Acceptance must:

- Mark one commentary message as `NON_TRADE`.
- Mark one conditional message as `NEEDS_CONTEXT`.
- Confirm neither changes verified holdings.
- Confirm decisions are persisted in `human_reviews`.
- Confirm any exported labels represent these as human-reviewed training candidates, not automatic production labels.

## 10. Idempotency

Acceptance must verify:

- Duplicate ingestion returns `DUPLICATE`.
- Duplicate approval returns `ALREADY_REVIEWED` or an equivalent safe result.
- Duplicate operations do not create new parser predictions.
- Duplicate operations do not create new shadow events.
- Duplicate operations do not create new verified events.
- Duplicate operations do not create extra human reviews.
- Duplicate operations do not change positions.

## 11. Concurrent Approval

Acceptance must:

- Simulate two reviewers approving one pending message.
- Confirm exactly one approval succeeds.
- Confirm the other approval returns `ALREADY_REVIEWED` or an equivalent safe result.
- Confirm exactly one verified event is created.
- Confirm exactly one verified position update occurs.
- Confirm transaction boundaries prevent partial duplicated writes.

## 12. Restart Persistence

Acceptance must:

- Create a pending review.
- Close database connections.
- Start a new process.
- Confirm the pending review remains visible.
- Confirm parser prediction details are still available.
- Confirm shadow and verified holdings are still readable.

## 13. Streamlit Smoke Test

Run the Streamlit application in headless mode.

Verify:

- it starts successfully
- the queue loads
- a review can be opened
- forms populate
- verified holdings display
- shadow holdings display
- rerunning the Streamlit script causes no database write

The smoke test must compare table counts before and after a script rerun where no reviewer action is submitted.

## 14. Human-Reviewed Data Export

Run:

```powershell
.\.venv\Scripts\python.exe -m src.export_verified_training_data --output data/verified_shadow_labels.csv
```

Verify `data/verified_shadow_labels.csv` is non-empty and includes:

- approved-as-parsed records
- corrected approvals
- rejected decisions where applicable
- non-trade decisions
- reviewer
- timestamps
- original ML predictions
- original rule predictions
- final human-reviewed event
- changed fields
- parser versions
- model versions

These records must be called human-reviewed training candidates, not perfect gold-standard data.

## 15. Shadow Metrics

Run `shadow_metrics`.

Generate non-empty reports for:

- action accuracy
- symbol accuracy
- percentage accuracy
- direction accuracy
- complete-event accuracy
- approval rate
- correction rate
- rejection rate
- manual-review rate
- verified/shadow position differences

The metrics implementation must explicitly document any metric that is approximated rather than field-perfect.

## 16. Database Integrity

Report table counts for:

- `incoming_messages`
- `parser_predictions`
- `human_reviews`
- `shadow_events`
- `verified_events`
- `verified_labels`
- `positions`

Acceptance must prove historical/default positions are unchanged by:

- capturing baseline `default` / historical position counts before shadow scenarios
- capturing baseline `default` / historical position row hashes before shadow scenarios
- comparing counts and hashes after all scenarios
- failing acceptance if historical/default positions changed unexpectedly

## 17. Required Tests

Ensure tests cover:

- shadow does not modify verified automatically
- approval updates verified
- edit-and-approve applies corrected values
- rejection leaves verified unchanged
- duplicate ingestion prevention
- duplicate approval prevention
- shadow and verified event coexistence
- prediction immutability
- latest verified context at approval
- percentage reduction correctness
- restart persistence
- migration idempotency
- foreign-key enforcement
- historical portfolio isolation
- missing-position reduction blocking
- verified import overwrite prevention
- corrected-label export
- concurrent approval safety
- Streamlit rerun safety

The coverage matrix must map each requirement to:

- test name
- implementation file
- scenario IDs used
- pass/fail result
- related report artifact

## 18. Required Reports

Generate:

- `reports/shadow_acceptance_report.md`
- `reports/shadow_acceptance_report.json`
- `reports/shadow_test_coverage_matrix.csv`
- `reports/shadow_acceptance_schema.csv`
- `reports/shadow_acceptance_table_counts.csv`
- `reports/shadow_acceptance_events.csv`
- `reports/shadow_acceptance_positions.csv`
- `reports/shadow_acceptance_reviews.csv`
- `reports/shadow_acceptance_revalidation.json`

Reports must identify:

- interpreter path
- Python version
- scikit-learn version
- Git branch
- Git commit
- database path
- acceptance classification
- failed requirements, if any

## 19. Acceptance Classification

Use only these classifications:

- `SHADOW_ACCEPTANCE_FAILED`
- `SHADOW_ACCEPTANCE_PARTIAL`
- `READY_FOR_MANUAL_SHADOW_USE`

Never classify this stage as automatic-production-ready.

`READY_FOR_MANUAL_SHADOW_USE` means the local shadow-testing and human-review workflow is acceptable for controlled manual review. It does not authorize unattended production updates.

## 20. Defect Handling

For every defect:

- identify root cause
- add a regression test
- fix it
- run the focused test
- rerun the complete suite
- document changed files
- document any database or report artifacts produced during investigation

Defect fixes must remain scoped to the failing requirement unless a broader change is explicitly approved.
