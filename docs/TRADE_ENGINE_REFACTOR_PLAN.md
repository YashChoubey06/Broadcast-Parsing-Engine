# Trade Engine Refactor Plan

Status: planning document only. Do not change source code, tests, schemas, datasets, or databases as part of this documentation task.

Manual shadow testing is paused until the confirmed semantics are implemented and re-accepted. Do not modify production or historical databases.

## Source Evidence Audit

The requested source-evidence path `data/raw/broadcast_admin.broadcasts_original.csv` was not present at inspection time. The available raw file inspected was:

```text
data/raw/broadcast_admin.broadcasts.csv
```

Treat the raw file as immutable source evidence. Do not edit, overwrite, clean in place, relabel in place, or commit modified contents.

Raw file fingerprint:

| Item | Value |
|---|---|
| Path inspected | `data/raw/broadcast_admin.broadcasts.csv` |
| Requested path not found | `data/raw/broadcast_admin.broadcasts_original.csv` |
| Row count | `1704` |
| SHA-256 | `824d76f16474c4dd08cf72e9382462169621510a4c861ed3bd941755f3ffad65` |
| Text column | `text` |

Columns:

```text
_id, segment, segmentName, text, attachment, createdBy, createdAt, updatedAt, __v,
media[0].url, media[0].key, media[0].originalName, media[0].mimeType,
media[0].size, attachment.url, attachment.key, attachment.originalName,
attachment.mimeType, attachment.size, media[0].kind, attachment.kind, editedAt
```

Observed source message patterns:

- Admin/non-trade chatter such as `Hello`, `why`, and `hi`.
- Single-clause entries such as buy/sell instructions with prices, SL, and targets.
- Profit-book and exit messages.
- Multi-clause messages joined by `&`, punctuation, or prose.
- Multi-instrument messages where one source row may affect more than one instrument.
- Multi-clause reversal candidates combining close/profit-book/exit language with opposite-side `BUY` or `SELL` entry language.

Confirmed multi-clause reversal evidence:

A scan found `16` candidate rows matching close/profit-book/exit language plus entry language in the same raw source text. A quick exact-text comparison found these candidates were not exact matches in `data/03_model_train_ready_deduplicated.csv` or `data/01_production_messages_final.csv`, supporting the user report that earlier cleaning removed multi-action messages.

Representative candidates from the raw file:

```text
FULL PROFIT BOOK IN HDFCBANK @785  & BUY HDFCBANK(JUNE) @777 & 755 SL 740 TGT 800-850
FULL PROFIT BOOK IN NIFTY @23980 & 50% SELL NIFTY (JUNE) @24050 SL 24200 TGT 23800 ...
FULL PROFIT BOOK IN CRUDE MINI @8550 & 50% SELL CRUDE MINI @8550 SL 8800 TGT 8300-8200
EXIT FROM SBIN & BUY SBIN@ 1017 SL 1005 TGT 1040
FULL PROFIT BOOK IN NIFTY@24140 & SELL 50% NIFTY@24150 SL 24250  TGT 24000 -23900
```

Do not generate the recovered reversal dataset yet.

## 1. Current Code Audit

### Parser Actions

Current action vocabulary includes `OPEN_LONG`, `OPEN_SHORT`, `ADD_LONG`, `ADD_SHORT`, `REDUCE_POSITION`, `CLOSE_POSITION`, status/update actions, review/control actions, and non-trade actions.

Current issue: the action names are usable, but the semantics attached to `SELL` and entry percentages are partly wrong. Existing acceptance scenarios treated `SELL 50% NIFTY` as `REDUCE_POSITION` against an existing long in some contexts. Confirmed semantics say standalone `SELL 50%` is a short entry/add based on customer buying capacity, unless explicit profit-book/exit/SL language closes or reduces an existing position.

### Rule Parser

`src/rule_parser.py` currently implements deterministic priorities and a sell ambiguity resolver. The current resolver has rules that can interpret existing-long + `SELL X%` as `REDUCE_POSITION` and existing-long + plain sell as close. These conflict with confirmed semantics.

Required changes later:

- Treat `BUY` as long entry/add and `SELL` as short entry/add.
- Use explicit profit-book/exit/SL-touch language for reductions and closes.
- Route standalone opposite-side entries against existing positions to `NEEDS_CONTEXT / OPPOSITE_DIRECTION_CONFLICT`.
- Parse ordered reversal clauses before applying standalone conflict rules.

### Hybrid Parser

`src/hybrid_parser.py` populates `ParsedTradeEvent`, applies defaults, and currently sets entry/add defaults to `DEFAULT_ENTRY_ALLOCATION_PCT` with `quantity_basis = MODEL_ALLOCATION`. It also validates percentages as 0-100.

Required changes later:

- Rename or reinterpret entry basis from `MODEL_ALLOCATION` to `CUSTOMER_BUYING_CAPACITY` while preserving backward compatibility.
- Allow cumulative position exposure above 100.
- Keep individual entry percentages bounded where appropriate, but do not reject a resulting position above 100.
- Add ordered multi-clause reversal parsing separate from the current conservative multi-instrument splitter.

### Holdings Engine

`src/holdings_engine.py` currently applies reductions correctly as current-position reductions. However, `_handle_add` caps allocation with `MAX_MODEL_ALLOCATION_PCT = 100`, and `_handle_open` sends duplicate same-side opens to review unless they are explicit add actions. Confirmed semantics require repeated same-side `BUY`/`SELL` entries to add capacity units and allow exposure above 100.

Required changes later:

- Replace the 100 cap for entry/add exposure.
- Treat same-side `OPEN_LONG` or `OPEN_SHORT` against an existing same-side position as an add when semantics resolve that way.
- Keep current-position reduction arithmetic unchanged.
- Keep full close behavior for explicit close instructions.

### Allocation Cap

`src/config.py` defines:

```text
MAX_MODEL_ALLOCATION_PCT = 100.0
```

This conflicts with confirmed repeated-entry semantics. It must be removed, renamed, or replaced with a different risk-control concept that does not cap valid cumulative exposure merely because it exceeds 100 customer-capacity units.

### Database Schema

The `positions` table already stores `portfolio_id`, `market_group`, `symbol`, `contract_month`, `option_type`, `strike_price`, and `direction`, but no current unique constraint enforces the full identity.

The `trade_events` table stores current action and quantity fields but does not explicitly separate surface instruction, entry capacity, reduction percentage, resolved side, sequence index, or parent message identity beyond existing parent/child fields.

### Training Labels

Existing label files encode older assumptions. `data/11_label_and_position_rules.csv` says `SELL X% without SL-target` is `REDUCE_POSITION` and entry basis is `MODEL_ALLOCATION`. That conflicts with confirmed semantics.

The existing sample review where `BUY 50% TSLA` was marked `NON_TRADE` is explicitly untrusted and must not be used as verified training data. `BUY 50% TSLA` should extract symbol `TSLA` and represent a long entry/add.

### Multi-Message Cleaning

The raw source evidence confirms multi-clause reversal candidates that appear to have been removed from cleaned training/production files. Future recovery must produce a new derived reviewed CSV and must not restore all removed rows automatically.

## 2. Proposed New Concepts

Add or introduce compatibility fields around these concepts:

- `surface_instruction`: literal instruction family observed in text, such as `BUY`, `SELL`, `PART_PROFIT`, `PROFIT_BOOK`, `FULL_PROFIT_BOOK`, `EXIT`, `SL_TOUCH`, `STOP_LOSS_HIT`.
- `entry_capacity_pct`: capacity units added for entry/add instructions.
- `reduction_pct`: percentage of current position to reduce.
- `quantity_basis`: retain existing field, but support `CUSTOMER_BUYING_CAPACITY` and `CURRENT_POSITION`; map old `MODEL_ALLOCATION` and `CURRENT_HOLDING` for compatibility.
- `existing_position_side`: `NONE`, `LONG`, `SHORT`, `BOTH`, or `UNKNOWN` at resolver time.
- `position_effect`: `OPEN`, `ADD`, `REDUCE`, `CLOSE`, `NO_CHANGE`, `REVIEW`.
- `resolved_position_side`: final side affected or created: `LONG`, `SHORT`, or `UNKNOWN`.
- `sequence_index`: ordered child index for multi-clause messages.
- `parent_message_id`: source parent ID shared by ordered children.

## 3. Backward-Compatible ParsedTradeEvent Changes

Keep existing fields so old records can still load:

- `final_action`
- `direction`
- `quantity_percent`
- `quantity_basis`
- `parent_source_message_id`
- `child_event_index`

Add optional fields with defaults:

- `surface_instruction: Optional[str]`
- `entry_capacity_pct: Optional[Decimal]`
- `reduction_pct: Optional[Decimal]`
- `existing_position_side: Optional[str]`
- `position_effect: Optional[str]`
- `resolved_position_side: Optional[str]`
- `sequence_index: Optional[int]`
- `parent_message_id: Optional[str]`

Compatibility mapping:

- Old `MODEL_ALLOCATION` entry fields map to `CUSTOMER_BUYING_CAPACITY`.
- Old `CURRENT_HOLDING` reduction fields map to `CURRENT_POSITION`.
- Existing `child_event_index` can map to or alias `sequence_index`.
- Existing `parent_source_message_id` can map to or alias `parent_message_id`.

## 4. State-Transition Resolver Design

Create a resolver layer between parsing and holdings application.

Inputs:

- Parsed surface instruction.
- Extracted instrument identity.
- Existing position side and size.
- Whether there are simultaneous sides.
- Clause sequence context.

Resolver output:

- `position_effect`
- `resolved_position_side`
- `entry_capacity_pct` or `reduction_pct`
- `quantity_basis`
- review reason if unsafe

Core rules:

- `BUY` resolves to `LONG` entry/add using customer capacity.
- `SELL` resolves to `SHORT` entry/add using customer capacity.
- Profit-book reductions resolve against current position.
- Explicit close instructions close the current position.
- Opposite-side standalone entries against an existing position route to review.
- Ordered reversal child 2 may open the opposite side only after child 1 close succeeds.
- Existing `BOTH` side state routes to review initially.

## 5. Ordered Multi-Clause Parser Design

Add a parser pass before normal single-event resolution:

1. Detect clause separators such as `&`, newlines, semicolons, and sentence boundaries.
2. Preserve original raw text and source row metadata.
3. Parse each clause independently for surface instruction, symbol, side, sizing, prices, SL, and targets.
4. Identify valid reversal pairs:
   - child 1 has close/reduction/full-profit/exit/SL-touch language for an existing side.
   - child 2 has opposite-side `BUY` or `SELL` entry language for the same instrument.
5. Emit ordered child events with one parent message ID and increasing sequence indexes.
6. Apply child 2 only if child 1 succeeds.
7. Route uncertain splits, symbol mismatches, multi-instrument ambiguity, or missing current position to human review.

The existing `parse_multi_instrument` conservative splitter can remain for uniform multi-instrument updates, but reversal splitting should be a separate design because sequence and state mutation matter.

## 6. Remove Or Replace MAX_MODEL_ALLOCATION_PCT = 100

Remove the use of `MAX_MODEL_ALLOCATION_PCT` as an exposure cap for entries/adds. Valid repeated entries can exceed 100.

Options:

- Rename to a non-blocking warning threshold, such as `EXPOSURE_WARNING_THRESHOLD`, and route only unusually large entries to review if separately approved.
- Keep no cap in the holdings engine and rely on explicit review rules for unsupported cases.

Do not cap cumulative exposure merely because it exceeds 100.

## 7. Database Migration Plan Preserving Existing Records

Migration principles:

- Preserve all existing rows and IDs.
- Add nullable columns; do not rewrite historical event meaning in place.
- Backfill compatibility fields where safe and mark approximations.
- Keep old JSON payloads readable.

Potential schema additions:

- `trade_events.surface_instruction`
- `trade_events.entry_capacity_pct`
- `trade_events.reduction_pct`
- `trade_events.position_effect`
- `trade_events.resolved_position_side`
- `trade_events.sequence_index`
- `trade_events.parent_message_id`
- Similar fields in shadow/verified event JSON or explicit shadow tables if needed.

Position identity migration:

- Add a full-key lookup path using `portfolio_id`, normalized `market_group`, `symbol`, `contract_month`, `option_type`, `strike_price`, and `direction`.
- Add indexes for full identity.
- Consider a partial unique index for open positions once duplicates are audited.
- Before enforcing uniqueness, generate a duplicate/conflict report.

## 8. Dataset Relabelling And Recovery Plan

Relabelling:

- Mark old labels based on the superseded `SELL reduces existing long` assumption as suspect.
- Remove the untrusted `BUY 50% TSLA -> NON_TRADE` sample from verified training use.
- Add reviewed examples for `BUY 50% TSLA` with symbol `TSLA`.
- Relabel entry percentages as customer-capacity units.
- Relabel reduction percentages as current-position reductions.

Future recovery script design:

1. Read only from the immutable raw source file, expected final path `data/raw/broadcast_admin.broadcasts_original.csv`; if the current file remains `data/raw/broadcast_admin.broadcasts.csv`, explicitly configure that path.
2. Compute and record row count, columns, and SHA-256 before processing.
3. Identify candidate multi-clause messages using separators such as `&`, newlines, semicolons, and sentence boundaries plus instruction keywords.
4. Preserve source `_id`, `createdAt`, `updatedAt`, `segment`, `segmentName`, raw `text`, and row number.
5. Parse candidate clauses without modifying the source row.
6. Split valid reversals into ordered child events with `parent_message_id`, `sequence_index`, and source metadata.
7. Require child 1 to be a close/current-position reduction and child 2 to be an opposite-side entry for the same instrument.
8. Route uncertain candidates to a human-review CSV instead of auto-restoring them.
9. Write a new derived CSV, for example `data/recovered_reversal_candidates_reviewed.csv`, without modifying the source file.
10. Include script-generated audit reports with candidate counts, accepted splits, rejected candidates, and review reasons.

Do not generate the recovered reversal dataset yet.

## 9. Test Plan

Parser tests:

- `BUY 50% TSLA` extracts symbol `TSLA` and resolves as long entry/add.
- `SELL 50% NIFTY` resolves as short entry/add when no existing long conflict blocks it.
- Profit-book phrases resolve as reductions using current position.
- Full profit, exit, SL touch, and stop-loss-hit resolve as full closes.
- No-percentage stock entries default to 100 capacity units.

Resolver tests:

- Existing long + same-side buy adds.
- Existing short + same-side sell adds.
- Existing long + standalone sell routes to opposite-direction conflict.
- Existing short + standalone buy routes to opposite-direction conflict.
- Simultaneous long and short routes to needs context.

Holdings tests:

- Repeated `BUY 50%` yields 50, 100, 150.
- Repeated `SELL 50%` yields short 50, 100, 150.
- Repeated `50% PROFIT BOOK` from 150 yields 75 then 37.5.
- Part profit from 150 yields 112.5.
- Same-side stock entries add 100 each time.
- No cap blocks cumulative exposure above 100.

Reversal tests:

- Close long then sell opens short only after close succeeds.
- Close short then buy opens long only after close succeeds.
- Child 2 does not run if child 1 fails.
- Parent message ID, timestamp, sequence index, and raw text are preserved.

Repository tests:

- Position lookup uses full identity.
- Same symbol in different markets does not collide.
- Futures contract months do not collide.
- Stock and option rows sharing symbol do not collide.
- Long and short side handling remains explicit.

Dataset tests:

- Recovery scanner identifies known raw reversal candidates.
- Recovery script writes only derived outputs.
- Raw source checksum remains unchanged.

## 10. Staged Implementation Plan

Use small reversible commits:

1. Add tests documenting confirmed semantics against current failures.
2. Add new optional ParsedTradeEvent fields with serialization compatibility.
3. Add state-transition resolver without wiring it into production flow.
4. Update rule parser to classify surface instructions separately from position effects.
5. Update hybrid parser defaults and quantity basis mapping.
6. Remove entry/add exposure cap behavior from holdings engine.
7. Implement guarded same-side add and opposite-side conflict behavior.
8. Implement ordered reversal parser and child event sequencing.
9. Add database migration for new nullable event fields and position-identity indexes.
10. Fix repository full-key position lookup after duplicate audit.
11. Add raw recovery scanner that produces audit-only reports.
12. Add reviewed derived reversal dataset generation in a later explicitly approved task.
13. Rerun full tests and shadow acceptance on an isolated database.
14. Update manual shadow runbook after re-acceptance.

## 11. Risks And Unresolved Items

- Historical reports and acceptance docs encode old semantics and will need clear versioning.
- Existing model labels may teach wrong `SELL` behavior.
- Reversal parsing can be ambiguous when one message includes multiple instruments.
- Market group normalization is not yet defined.
- Same symbol can exist in multiple markets or instruments.
- Simultaneous long and short support is intentionally deferred.
- Existing shadow acceptance results are no longer semantically sufficient.
- Recovery from raw data must be reviewed; do not restore all removed rows automatically.
- The exact requested raw filename was not present during this inspection; align the source path before automating recovery.

## 12. Modules That Can Remain Largely Unchanged

Likely unchanged or minimally changed:

- `src/text_normalizer.py`, except if clause splitting needs normalization support.
- `src/database.py` base connection handling.
- Repository protocol shape can remain but implementation must honor full identity fields.
- Snapshot persistence can remain.
- Export/report plumbing can remain, with added fields where useful.
- Streamlit review shell can remain, but forms may need new fields for entry capacity, reduction percentage, surface instruction, and sequence metadata.
- Existing Decimal arithmetic approach should remain.

## Confirmed Position-Identity Defect

Current documented position key is:

```text
portfolio_id + market_group + symbol + contract_month + option_type + strike_price + direction
```

Current effective SQLite lookup mainly matches:

```text
portfolio_id + symbol + optional direction
```

The repository accepts `market_group`, `contract_month`, `option_type`, and `strike_price` parameters, but the SQL ignores them. Shadow helper queries also use symbol-only or symbol/direction-only lookups.

Risk:

- Same symbol in multiple markets can select the wrong holding.
- Multiple contract months can collide.
- Stock and option positions sharing the same symbol can collide.
- Long/short is only partly protected; missing direction can still select the wrong side.

Recommendation:

- Implement full-key lookup with explicit market/instrument normalization.
- Permit fallback only when exactly one open candidate exists.
- Route multiple matches to `NEEDS_CONTEXT`.
- Add duplicate/conflict reports before adding uniqueness constraints.
