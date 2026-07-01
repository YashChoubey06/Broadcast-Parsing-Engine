# Manual Shadow Operations Runbook

This runbook is for controlled manual shadow use after acceptance status
`READY_FOR_MANUAL_SHADOW_USE`.

Manual shadow use must remain isolated from historical and acceptance data. The
manual database for this workflow is:

```text
storage/shadow_manual.db
```

## Confirm The Baseline

From the project root:

```powershell
cd "C:\Astrodunia text parsing\trade_message_system"
git status --short
git tag --list shadow-acceptance-v1
git show --stat --oneline shadow-acceptance-v1
```

Expected tag:

```text
shadow-acceptance-v1
```

The accepted report is `reports/shadow_acceptance_report.md`, and its
classification must remain `READY_FOR_MANUAL_SHADOW_USE`. This is not approval
for unattended production updates.

## Safety Rules

- Never automatically update verified holdings.
- Never use `storage/shadow_acceptance.db` for real messages.
- Never reset historical databases:
  - `storage/trade_holdings.db`
  - `storage/trade_holdings_audit.db`
  - any dated or previously used shadow database
- Always inspect the current database path before import or migration.
- Use one writer at a time. SQLite is for local, limited single-writer use.
- Shut down Streamlit and stop ingestion/import commands before copying,
  backing up, restoring, or replacing a database file.
- Treat exported labels as human-reviewed training candidates, not perfect
  gold-standard data.

## Database Path Setup

Several CLIs accept `--db`. The Streamlit app, holdings display, review queue,
single-message ingestion, and batch ingestion read `TRADE_DB_PATH` through
`src.config.DATABASE_PATH`.

For each new PowerShell session used for manual shadow work:

```powershell
$env:TRADE_DB_PATH = "storage/shadow_manual.db"
.\.venv\Scripts\python.exe -c "from src.config import DATABASE_PATH; print(DATABASE_PATH)"
```

The printed path must be `storage\shadow_manual.db` or the absolute equivalent.
Do not continue if it prints `storage\trade_holdings.db` or
`storage\shadow_acceptance.db`.

## 1. Create A New Isolated Manual Database

Do not create this database until real manual shadow use is starting.

```powershell
$env:TRADE_DB_PATH = "storage/shadow_manual.db"
.\.venv\Scripts\python.exe -c "from src.config import DATABASE_PATH; print(DATABASE_PATH)"
.\.venv\Scripts\python.exe -m src.database
```

`src.database` creates the base tables using the configured `TRADE_DB_PATH`.

## 2. Run Migrations Against The Manual Database

Inspect the path first, then apply the shadow migration explicitly:

```powershell
$env:TRADE_DB_PATH = "storage/shadow_manual.db"
.\.venv\Scripts\python.exe -c "from src.config import DATABASE_PATH; print(DATABASE_PATH)"
.\.venv\Scripts\python.exe -m scripts.migrate_shadow_tables --db storage/shadow_manual.db
```

The migration is idempotent. A second run should report that
`001_shadow_testing_tables` is already applied:

```powershell
.\.venv\Scripts\python.exe -m scripts.migrate_shadow_tables --db storage/shadow_manual.db
```

## 3. Prepare The Current Verified Holdings CSV

Use `data/verified_initial_positions_template.csv` as a header reference only.
Create a separate real CSV for the current verified portfolio, for example:

```text
data/verified_initial_positions_current.csv
```

Required columns used by the importer:

```text
symbol,direction
```

Supported optional columns:

```text
market_group,contract_month,option_type,strike_price,current_allocation_pct,average_entry_price,stop_loss,targets,status,as_of_timestamp
```

The importer always writes to the `verified` portfolio, regardless of CSV
contents. Do not include confidential data in template files.

## 4. Dry-Run The Verified-Position Import

```powershell
.\.venv\Scripts\python.exe -m src.import_verified_positions `
  --input data/verified_initial_positions_current.csv `
  --dry-run `
  --db storage/shadow_manual.db
```

Read every proposed `+ DIRECTION SYMBOL @ allocation%` line before applying.

## 5. Apply The Import With Explicit Confirmation

```powershell
.\.venv\Scripts\python.exe -m src.import_verified_positions `
  --input data/verified_initial_positions_current.csv `
  --apply `
  --confirm `
  --db storage/shadow_manual.db
```

The importer prevents silent overwrite of an existing open verified position
with the same `symbol` and `direction`.

## 6. Start The Streamlit Review Application

Set the database path in the same PowerShell session used to start Streamlit:

```powershell
$env:TRADE_DB_PATH = "storage/shadow_manual.db"
.\.venv\Scripts\python.exe -c "from src.config import DATABASE_PATH; print(DATABASE_PATH)"
.\.venv\Scripts\python.exe -m streamlit run app/shadow_review_app.py
```

Before reviewing anything, confirm the app shows the expected verified and
shadow holdings for the manual database.

## 7. Ingest One Message Through The CLI

The single-message ingestion CLI reads `TRADE_DB_PATH`; it does not expose a
`--db` argument.

```powershell
$env:TRADE_DB_PATH = "storage/shadow_manual.db"
.\.venv\Scripts\python.exe -c "from src.config import DATABASE_PATH; print(DATABASE_PATH)"
.\.venv\Scripts\python.exe -m src.ingest_shadow_message `
  --message-id manual-YYYYMMDD-001 `
  --text "MESSAGE TEXT HERE" `
  --segment "Manual"
```

Use a unique `--message-id` for each source message.

## 8. Ingest A Batch CSV Of Messages

Use `data/shadow_message_batch_template.csv` as a header reference only. The
batch reader accepts either:

```text
source_message_id,text,segment
```

or compatible aliases:

```text
id,raw_text,market
```

Run:

```powershell
$env:TRADE_DB_PATH = "storage/shadow_manual.db"
.\.venv\Scripts\python.exe -c "from src.config import DATABASE_PATH; print(DATABASE_PATH)"
.\.venv\Scripts\python.exe -m src.ingest_shadow_csv --input data/shadow_message_batch_current.csv
```

The command reports successfully ingested messages and duplicates ignored.

## 9. Review Messages

Reviews are performed in the Streamlit app.

Available review actions:

- `Approve as Parsed`: records `APPROVED` and applies the parser prediction to
  the verified portfolio after revalidation.
- `Apply Manual Correction`: edits the event fields in the correction form,
  records `APPROVED_WITH_CORRECTION`, and applies the corrected event.
- `Reject`: records `REJECTED`; verified holdings do not change.
- `Mark Non-Trade`: records `NON_TRADE`; verified holdings do not change.
- `Needs Context`: records `NEEDS_CONTEXT`; verified holdings do not change.

If the app says the message is no longer found or already reviewed, refresh the
queue before continuing.

## 10. Show Verified Holdings

`src.show_holdings` reads `TRADE_DB_PATH`; it does not expose a `--db` argument.

```powershell
$env:TRADE_DB_PATH = "storage/shadow_manual.db"
.\.venv\Scripts\python.exe -c "from src.config import DATABASE_PATH; print(DATABASE_PATH)"
.\.venv\Scripts\python.exe -m src.show_holdings --portfolio verified
```

Include closed positions when needed:

```powershell
.\.venv\Scripts\python.exe -m src.show_holdings --portfolio verified --all
```

## 11. Show Shadow Holdings

```powershell
$env:TRADE_DB_PATH = "storage/shadow_manual.db"
.\.venv\Scripts\python.exe -m src.show_holdings --portfolio shadow
```

Include closed positions when needed:

```powershell
.\.venv\Scripts\python.exe -m src.show_holdings --portfolio shadow --all
```

## 12. Compare Verified And Shadow Holdings

The Streamlit app shows verified and shadow holdings side by side.

For file output, run shadow metrics against the manual database:

```powershell
.\.venv\Scripts\python.exe -m src.shadow_metrics `
  --db storage/shadow_manual.db `
  --out-dir reports/manual_shadow_metrics
```

Inspect:

```text
reports/manual_shadow_metrics/shadow_position_differences.csv
reports/manual_shadow_metrics/shadow_testing_summary.json
```

## 13. Export Human-Reviewed Training Candidates

```powershell
.\.venv\Scripts\python.exe -m src.export_verified_training_data `
  --db storage/shadow_manual.db `
  --output data/verified_shadow_labels_manual.csv
```

The output is human-reviewed training candidates. Do not treat it as automatic
production truth.

## 14. Generate Shadow Metrics

```powershell
.\.venv\Scripts\python.exe -m src.shadow_metrics `
  --db storage/shadow_manual.db `
  --out-dir reports/manual_shadow_metrics
```

Expected report files include:

- `shadow_testing_summary.json`
- `shadow_testing_summary.csv`
- `shadow_action_metrics.csv`
- `shadow_entity_metrics.csv`
- `shadow_review_reason_counts.csv`
- `shadow_position_differences.csv`
- `shadow_false_automatic_events.csv`

## 15. Back Up The SQLite Database

Stop Streamlit and wait for all ingestion, import, export, and metrics commands
to finish. Then copy the database and SQLite sidecar files if present:

```powershell
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
Copy-Item -Path "storage/shadow_manual.db" -Destination "storage/shadow_manual.$timestamp.db.bak"
if (Test-Path "storage/shadow_manual.db-wal") { Copy-Item -Path "storage/shadow_manual.db-wal" -Destination "storage/shadow_manual.$timestamp.db-wal.bak" }
if (Test-Path "storage/shadow_manual.db-shm") { Copy-Item -Path "storage/shadow_manual.db-shm" -Destination "storage/shadow_manual.$timestamp.db-shm.bak" }
```

Keep backups in `storage/` or another approved secure location.

## 16. Restore The SQLite Database

Stop Streamlit and all commands first. Back up the current file before
restoring over it.

```powershell
$restore = "storage/shadow_manual.YYYYMMDD-HHMMSS.db.bak"
Copy-Item -Path "storage/shadow_manual.db" -Destination "storage/shadow_manual.before-restore.db.bak"
Copy-Item -Path $restore -Destination "storage/shadow_manual.db"
```

If matching `-wal` or `-shm` backup files are being restored, restore them as a
consistent set from the same timestamp. Do not mix sidecar files from different
backup times.

After restore:

```powershell
$env:TRADE_DB_PATH = "storage/shadow_manual.db"
.\.venv\Scripts\python.exe -m src.show_holdings --portfolio verified
.\.venv\Scripts\python.exe -m src.show_holdings --portfolio shadow
```

## 17. Shut Down Safely

1. Stop message ingestion and import commands.
2. Finish or cancel the current review action in Streamlit.
3. Stop Streamlit with `Ctrl+C` in its terminal.
4. Run a final backup if any reviews or imports were applied.
5. Leave `storage/shadow_manual.db` in place. Do not reset it.

## 18. Troubleshooting

`database is locked`

- Stop extra Streamlit or Python processes using the same database.
- Avoid batch ingestion while reviewing.
- Retry after the active writer exits.
- If backing up or restoring, shut down Streamlit first.

`DUPLICATE` or `Duplicates ignored`

- The same `source_message_id` is already present.
- Use the existing review item instead of re-ingesting.
- If the source message is genuinely different, assign a new unique message ID.

`ALREADY_REVIEWED`

- The message already has a human review.
- Refresh the Streamlit queue and inspect exported labels or metrics instead of
  attempting a second approval.

Missing verified position

- Reductions and closes need an existing verified holding at approval time.
- Inspect verified holdings:

```powershell
$env:TRADE_DB_PATH = "storage/shadow_manual.db"
.\.venv\Scripts\python.exe -m src.show_holdings --portfolio verified
```

- Import the real current verified position only through the dry-run and
  confirmed apply workflow above.

Ambiguous sell

- A sell or reduction can require current holding context.
- Review the parser prediction in Streamlit.
- If the verified position exists and the parser action is correct, approve or
  edit and approve. Otherwise mark `Needs Context` or reject.

Missing model

- The parser expects the saved model under `models/`.
- Confirm the model files exist before ingestion:

```powershell
Test-Path models/action_classifier.joblib
Test-Path models/model_metadata.json
```

- Do not retrain during manual shadow operation unless a separate model update
  task has been approved.

Wrong database path

- Always print the configured path before import, migration, Streamlit, or
  ingestion:

```powershell
.\.venv\Scripts\python.exe -c "from src.config import DATABASE_PATH; print(DATABASE_PATH)"
```

- If the path is wrong, set it again:

```powershell
$env:TRADE_DB_PATH = "storage/shadow_manual.db"
```

## 19. Daily Operating Checklist

Before starting:

- Confirm the Git tag exists:

```powershell
git tag --list shadow-acceptance-v1
```

- Set and inspect the database path:

```powershell
$env:TRADE_DB_PATH = "storage/shadow_manual.db"
.\.venv\Scripts\python.exe -c "from src.config import DATABASE_PATH; print(DATABASE_PATH)"
```

- Start Streamlit with the manual database path set.
- Show verified holdings and confirm they match the current verified portfolio.
- Show shadow holdings and note any expected differences.

During operation:

- Use unique message IDs.
- Keep one writer active at a time.
- Review every pending message manually.
- Use `Needs Context` for conditional, unclear, missing-position, or
  prior-message-dependent instructions.
- Never let shadow holdings overwrite verified holdings.

End of day:

- Export reviewed training candidates.
- Generate shadow metrics.
- Compare verified and shadow differences.
- Stop Streamlit.
- Back up `storage/shadow_manual.db`.
- Record any operational issues and unresolved `Needs Context` items.
