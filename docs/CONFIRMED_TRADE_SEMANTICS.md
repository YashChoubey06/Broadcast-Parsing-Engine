# Confirmed Trade Semantics

Status: authoritative business rules confirmed by the signal author.

These rules supersede earlier assumptions in the parser, training labels, and acceptance scenarios where they conflict. The original broadcast CSV is authoritative source evidence for recovering removed messages, but these rules are authoritative for interpreting those messages.

## Definitions

- `CUSTOMER_BUYING_CAPACITY`: the sizing basis for entries and additions. A `50%` entry means add 50 capacity units, not reduce or resize the current position.
- `CURRENT_POSITION`: the sizing basis for reductions and closes. A `50% PROFIT BOOK` reduces the current open position by 50% of its current size.
- `LONG`: exposure created or increased by `BUY` instructions.
- `SHORT`: exposure created or increased by `SELL` instructions.
- `Entry instruction`: `BUY`, `SELL`, `BUY X%`, `SELL X%`, same-side repeated entries, and stock entries with no explicit percentage.
- `Reduction instruction`: `PART PROFIT`, `X% PROFIT BOOK`, and equivalent profit-book language.
- `Close instruction`: `FULL PROFIT BOOK`, `EXIT FROM X`, `SL TOUCH IN X`, and `STOP LOSS HIT`.
- `ORDERED_CLOSE_THEN_ENTRY`: an ordered pair of clauses in a single message where the first clause fully closes the current database position and the second opens a new `BUY` or `SELL` entry for the same non-direction instrument identity.
- `Reversal`: the portfolio-context outcome when `ORDERED_CLOSE_THEN_ENTRY` closes a prior `LONG` then opens `SELL`, or closes a prior `SHORT` then opens `BUY`.
- `SAME_SIDE_REENTRY`: the portfolio-context outcome when `ORDERED_CLOSE_THEN_ENTRY` closes a prior `LONG` then opens `BUY`, or closes a prior `SHORT` then opens `SELL`.
- `NEEDS_CONTEXT / OPPOSITE_DIRECTION_CONFLICT`: the safe state for standalone opposite-side entries against an existing position.

## Entry Versus Reduction Percentage Semantics

Entry percentages use `CUSTOMER_BUYING_CAPACITY`.

- `BUY 50%` means add 50 capacity units to a `LONG` position.
- `SELL 50%` means add 50 capacity units to a `SHORT` position.
- Same-side repeated entries add more capacity units.
- Entry exposure may exceed 100 capacity units.
- No event may be rejected or capped merely because cumulative exposure exceeds 100.

Reduction percentages use `CURRENT_POSITION`.

- `PART PROFIT` always reduces 25% of the current position.
- `50% PROFIT BOOK` reduces 50% of the current position.
- Repeated reductions compound on the remaining current position.

## Stock Default Sizing

For individual stock entries, no explicit percentage means full-capacity entry.

- `BUY NVDA @1234 SL 1200 TGT 1300-1350` opens or adds `LONG 100` capacity units.
- `SELL NVDA` opens or adds `SHORT 100` capacity units.
- If the same-side stock position already exists, another no-percentage same-side stock entry adds another 100 capacity units.

The system does not use `BUY X LOT` or `SELL X LOT` as sizing. Supported sizing surfaces are `PART`, explicit percentages such as `50%`, `FULL`, or no percentage for a full stock entry.

## Complete Transition Matrix

| Existing position | Incoming surface instruction | Meaning | Required effect |
|---|---|---|---|
| None | `BUY X%` | Long entry | Open `LONG` with `entry_capacity_pct = X` |
| None | `BUY` stock with no % | Full long stock entry | Open `LONG` with `entry_capacity_pct = 100` |
| None | `SELL X%` | Short entry | Open `SHORT` with `entry_capacity_pct = X` |
| None | `SELL` stock with no % | Full short stock entry | Open `SHORT` with `entry_capacity_pct = 100` |
| None | `PART PROFIT` | Reduction without prior position | `NEEDS_CONTEXT` / missing prior position |
| None | `X% PROFIT BOOK` | Reduction without prior position | `NEEDS_CONTEXT` / missing prior position |
| None | `FULL PROFIT BOOK`, `EXIT`, `SL TOUCH`, `STOP LOSS HIT` | Close without prior position | `NEEDS_CONTEXT` / missing prior position |
| Existing `LONG` | `BUY X%` | Same-side add | Add `X` capacity units to `LONG` |
| Existing `LONG` | `BUY` stock with no % | Same-side full add | Add `100` capacity units to `LONG` |
| Existing `LONG` | `SELL X%` standalone | Opposite-side entry without explicit close | `NEEDS_CONTEXT` / `OPPOSITE_DIRECTION_CONFLICT` |
| Existing `LONG` | `SELL` standalone | Opposite-side entry without explicit close | `NEEDS_CONTEXT` / `OPPOSITE_DIRECTION_CONFLICT` |
| Existing `LONG` | `PART PROFIT` | Reduce current long | `LONG = LONG * 0.75` |
| Existing `LONG` | `X% PROFIT BOOK` | Reduce current long | `LONG = LONG * (1 - X/100)` |
| Existing `LONG` | `FULL PROFIT BOOK`, `EXIT`, `SL TOUCH`, `STOP LOSS HIT` | Close current long | Close `LONG` to zero |
| Existing `LONG` | close clause then `SELL X%` clause | Ordered close then entry, portfolio outcome `REVERSAL` | Child 1 closes `LONG`; child 2 opens `SHORT X` only after child 1 succeeds |
| Existing `LONG` | close clause then `BUY X%` clause | Ordered close then entry, portfolio outcome `SAME_SIDE_REENTRY` | Child 1 closes `LONG`; child 2 opens `LONG X` only after child 1 succeeds |
| Existing `SHORT` | `SELL X%` | Same-side add | Add `X` capacity units to `SHORT` |
| Existing `SHORT` | `SELL` stock with no % | Same-side full add | Add `100` capacity units to `SHORT` |
| Existing `SHORT` | `BUY X%` standalone | Opposite-side entry without explicit close | `NEEDS_CONTEXT` / `OPPOSITE_DIRECTION_CONFLICT` |
| Existing `SHORT` | `BUY` standalone | Opposite-side entry without explicit close | `NEEDS_CONTEXT` / `OPPOSITE_DIRECTION_CONFLICT` |
| Existing `SHORT` | `PART PROFIT` | Reduce current short | `SHORT = SHORT * 0.75` |
| Existing `SHORT` | `X% PROFIT BOOK` | Reduce current short | `SHORT = SHORT * (1 - X/100)` |
| Existing `SHORT` | `FULL PROFIT BOOK`, `EXIT`, `SL TOUCH`, `STOP LOSS HIT` | Close current short | Close `SHORT` to zero |
| Existing `SHORT` | close clause then `BUY X%` clause | Ordered close then entry, portfolio outcome `REVERSAL` | Child 1 closes `SHORT`; child 2 opens `LONG X` only after child 1 succeeds |
| Existing `SHORT` | close clause then `SELL X%` clause | Ordered close then entry, portfolio outcome `SAME_SIDE_REENTRY` | Child 1 closes `SHORT`; child 2 opens `SHORT X` only after child 1 succeeds |
| Existing `LONG` and `SHORT` same instrument | Any new instruction | Rare unsupported simultaneous sides | Route to `NEEDS_CONTEXT` initially |

## Repeated Entries Above 100

Repeated same-side entries add capacity units and may exceed 100.

Example:

```text
BUY 50%
BUY 50%
BUY 50%
```

Required state transitions:

```text
LONG 50 -> LONG 100 -> LONG 150
```

A cap at 100 is incorrect for entry/add semantics.

## Reduction Examples

Current position:

```text
LONG 150
```

Repeated 50% profit-book reductions:

```text
50% PROFIT BOOK -> LONG 75
50% PROFIT BOOK -> LONG 37.5
```

Part-profit reductions:

```text
PART PROFIT from 150 -> 112.5
PART PROFIT again -> 84.375
```

Full close:

```text
FULL PROFIT BOOK IN X -> 0
EXIT FROM X -> 0
SL TOUCH IN X -> 0
STOP LOSS HIT IN X -> 0
```

## Ordered Close-Then-Entry Sequencing

Ordered close-then-entry sequences may be expressed as two ordered clauses in one source message.

Example:

```text
FULL PROFIT BOOK IN X @1124
&
50% SELL X @1124 SL 1200 TGT 1100-1050
```

Text-level requirements:

1. Child event 1 is an explicit full-close instruction, such as `FULL PROFIT BOOK`, `EXIT`, `SL TOUCH`, or `STOP LOSS HIT`.
2. Child event 2 is an explicit `BUY` or `SELL` entry.
3. Both clauses refer to the same full non-direction instrument identity.
4. Clause ordering is clear.

The close clause normally does not state `LONG` or `SHORT`. It means close the
currently open database position for that exact instrument identity. Therefore,
the text parser can confirm `ORDERED_CLOSE_THEN_ENTRY`, but cannot classify the
portfolio outcome as `REVERSAL` or `SAME_SIDE_REENTRY` without trustworthy
database position context.

Runtime ordered effects:

1. Child event 1 closes the existing `LONG X`.
2. Child event 2 opens `SHORT X` with 50 customer-capacity units.
3. Child event 2 must run only after child event 1 succeeds.
4. The original parent message ID, timestamp, and source text must be preserved.

The database position determines the final portfolio outcome:

- previous `LONG` + new `SELL` = `REVERSAL`
- previous `SHORT` + new `BUY` = `REVERSAL`
- previous `LONG` + new `BUY` = `SAME_SIDE_REENTRY`
- previous `SHORT` + new `SELL` = `SAME_SIDE_REENTRY`

For raw Phase 4 historical text-only review, `portfolio_effect_status` defaults
to `NOT_EVALUATED` unless a trustworthy historical database snapshot is
explicitly available. Do not infer `REVERSAL` from text alone.

## Ambiguous And Untrusted Cases

- Existing `SHORT` + standalone `BUY` is not an automatic cover. Route to `NEEDS_CONTEXT / OPPOSITE_DIRECTION_CONFLICT` unless an explicit close clause precedes it.
- Existing `LONG` + standalone `SELL` is not an automatic long reduction. Route to `NEEDS_CONTEXT / OPPOSITE_DIRECTION_CONFLICT` unless profit-book/exit/SL language is present or an explicit close clause precedes it.
- Simultaneous `LONG` and `SHORT` positions for the same instrument are rare and may remain unsupported initially. Route to `NEEDS_CONTEXT`.
- The existing sample review where `BUY 50% TSLA` was marked `NON_TRADE` is not trusted and must not be used as verified training data.
- The current symbol extraction failure for `BUY 50% TSLA` is a parser defect. The symbol should be `TSLA`.

## Worked Numerical Examples

Long entries:

```text
Start: no AAPL position
BUY 50% AAPL -> LONG 50
BUY 50% AAPL -> LONG 100
BUY AAPL -> LONG 200 if no percentage defaults to stock full entry of 100
```

Short entries:

```text
Start: no NIFTY position
SELL 50% NIFTY -> SHORT 50
SELL 50% NIFTY -> SHORT 100
SELL 50% NIFTY -> SHORT 150
```

Long reduction:

```text
Start: LONG 150
50% PROFIT BOOK IN AAPL -> LONG 75
PART PROFIT IN AAPL -> LONG 56.25
EXIT FROM AAPL -> closed
```

Opposite-side conflict:

```text
Start: LONG 100 INFY
SELL INFY @1600 SL 1650 TGT 1500 -> NEEDS_CONTEXT / OPPOSITE_DIRECTION_CONFLICT
```

Valid ordered close-then-entry:

```text
Start: LONG 100 INFY
FULL PROFIT BOOK IN INFY @1600 & SELL INFY @1600 SL 1650 TGT 1500
child 1 -> close LONG INFY
child 2 -> open SHORT INFY 100
```

The example above has portfolio outcome `REVERSAL` only because the prior
database position is known to be `LONG`. With prior `SHORT` and a following
`SELL`, the same text-level structure would be a valid `SAME_SIDE_REENTRY`.
