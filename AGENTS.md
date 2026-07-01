# Trade Message System — Agent Instructions

## Project purpose

This repository implements:

1. A hybrid scikit-learn and deterministic-rule trade-message parser.
2. A deterministic holdings engine.
3. SQLite-based historical replay.
4. A local shadow-testing and human-review workflow.
5. Separate historical, shadow and verified portfolios.

## Critical business rule

All reductions apply to the current holding:

new_holding = current_holding * (1 - reduction_percent / 100)

Examples:

- 100 reduced by 50% = 50
- 50 reduced by 50% = 25
- 25 reduced by 25% = 18.75

Use Decimal arithmetic for holdings calculations.

## Portfolio separation

The shared positions table uses portfolio_id:

- default or historical
- shadow
- verified

Shadow events must never automatically update verified holdings.

Verified holdings change only after explicit human approval.

## Existing project state

The implementation currently includes:

- scikit-learn action classifier
- TF-IDF word and character features
- Logistic Regression runtime classifier
- regex and alias-based entity extraction
- hybrid rule/ML resolver
- validation layer
- deterministic holdings engine
- SQLite repositories
- historical replay
- dry-run and safe-apply modes
- Streamlit shadow-review application
- message ingestion CLI
- initial verified-position importer
- human review workflow
- verified-label export
- shadow metrics
- database migrations

Reported latest test state:

- 128 tests passing
- 116 original tests
- 12 shadow-workflow tests

Do not trust this number without rerunning the suite.

## Existing model metrics

Pilot test results:

- Accuracy: approximately 0.8222
- Weighted F1: approximately 0.8400
- Macro F1: approximately 0.5418

This model is not approved for unattended production updates.

## Historical audit state

Historical dataset:

- 1,043 source messages
- 505 automatically applied
- 490 manual review
- 48 status-only or skipped
- 49 parser/dataset disagreements
- zero reported dry-run versus safe-apply differences
- zero reported new events during second replay

Historical holdings are only positions reconstructable from the supplied period.

## Shadow workflow principles

- Original parser predictions are immutable.
- Edited approvals use standard trade actions.
- Human correction metadata is stored separately.
- Approval must revalidate against the latest verified holding.
- A rejected prediction remains visible in shadow history.
- Duplicate ingestion must not create duplicate predictions.
- Duplicate approval must not create duplicate verified events.
- SQLite is suitable for limited local single-writer usage, not high-concurrency production.
- Streamlit session state is never the database source of truth.

## Safety requirements

Do not automatically apply:

- corrections requiring prior-message context
- conditional instructions
- unresolved multi-instrument messages
- missing-symbol events
- ambiguous sell instructions
- missing-prior-position reductions
- opposite-direction conflicts
- low-confidence unsafe predictions

## Files that must not be overwritten

Do not modify the original dataset files under data/.

Do not delete historical reports.

Do not reset these databases unless explicitly instructed:

- storage/trade_holdings.db
- storage/trade_holdings_audit.db

For acceptance testing use:

- storage/shadow_acceptance.db

## Development requirements

Before changing code:

1. Run git status.
2. Inspect recent commits.
3. Read README.md and walkthrough.md.
4. Inspect database migrations.
5. Run the complete test suite.
6. Report the current state.

For every defect fixed:

1. Add or update a regression test.
2. Run the relevant test.
3. Run the complete test suite.
4. Explain the root cause and changed files.

Do not restyle or rewrite working modules unnecessarily.