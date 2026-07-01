import sqlite3
import hashlib
from datetime import datetime
from pathlib import Path

from src.config import DATABASE_PATH
from src.database import get_connection

from contextlib import closing

def apply_migration(db_path: Path | None = None):
    if db_path is None:
        db_path = DATABASE_PATH
    with closing(get_connection(db_path)) as conn:
        cursor = conn.cursor()
        
        # 1. Create schema_migrations table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                migration_id INTEGER PRIMARY KEY AUTOINCREMENT,
                migration_name TEXT NOT NULL UNIQUE,
                applied_at TEXT NOT NULL,
                checksum TEXT NOT NULL
            );
        """)
        
        migration_name = "001_shadow_testing_tables"
        cursor.execute("SELECT 1 FROM schema_migrations WHERE migration_name = ?", (migration_name,))
        if cursor.fetchone():
            print(f"Migration {migration_name} already applied.")
            return

        print(f"Applying migration: {migration_name}...")
        
        # Migration SQL
        sql = """
            -- Incoming messages
            CREATE TABLE IF NOT EXISTS incoming_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_message_id TEXT NOT NULL UNIQUE,
                raw_text TEXT NOT NULL,
                normalized_text TEXT,
                segment_name TEXT,
                created_at TEXT NOT NULL,
                received_at TEXT NOT NULL,
                processing_status TEXT NOT NULL,
                text_hash TEXT
            );

            -- Parser predictions
            CREATE TABLE IF NOT EXISTS parser_predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_message_id TEXT NOT NULL,
                parser_version TEXT NOT NULL,
                model_version TEXT NOT NULL,
                prediction_json TEXT NOT NULL,
                ml_action TEXT,
                ml_confidence REAL,
                rule_action TEXT,
                final_action TEXT,
                symbol TEXT,
                quantity_percent TEXT,
                validation_status TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (source_message_id) REFERENCES incoming_messages(source_message_id),
                UNIQUE(source_message_id, parser_version, model_version)
            );

            -- Human reviews
            CREATE TABLE IF NOT EXISTS human_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_message_id TEXT NOT NULL,
                prediction_id INTEGER NOT NULL,
                reviewer TEXT,
                decision TEXT NOT NULL,
                original_prediction_json TEXT NOT NULL,
                corrected_event_json TEXT,
                changed_fields_json TEXT,
                review_notes TEXT,
                reviewed_at TEXT NOT NULL,
                FOREIGN KEY (source_message_id) REFERENCES incoming_messages(source_message_id),
                FOREIGN KEY (prediction_id) REFERENCES parser_predictions(id)
            );

            -- Shadow events
            CREATE TABLE IF NOT EXISTS shadow_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_message_id TEXT NOT NULL UNIQUE,
                parsed_event_json TEXT NOT NULL,
                position_before_json TEXT,
                position_after_json TEXT,
                status TEXT NOT NULL,
                applied_at TEXT NOT NULL,
                FOREIGN KEY (source_message_id) REFERENCES incoming_messages(source_message_id)
            );

            -- Verified events
            CREATE TABLE IF NOT EXISTS verified_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_message_id TEXT NOT NULL UNIQUE,
                approved_event_json TEXT NOT NULL,
                position_before_json TEXT,
                position_after_json TEXT,
                review_id INTEGER,
                applied_at TEXT NOT NULL,
                FOREIGN KEY (source_message_id) REFERENCES incoming_messages(source_message_id),
                FOREIGN KEY (review_id) REFERENCES human_reviews(id)
            );

            -- Verified labels
            CREATE TABLE IF NOT EXISTS verified_labels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_message_id TEXT NOT NULL UNIQUE,
                raw_text TEXT NOT NULL,
                verified_record_type TEXT,
                verified_action TEXT,
                verified_symbol TEXT,
                verified_direction TEXT,
                verified_quantity_percent TEXT,
                verified_quantity_basis TEXT,
                verified_entry_prices_json TEXT,
                verified_stop_loss TEXT,
                verified_targets_json TEXT,
                original_ml_action TEXT,
                original_ml_confidence REAL,
                original_rule_action TEXT,
                was_corrected INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY (source_message_id) REFERENCES incoming_messages(source_message_id)
            );
        """
        
        cursor.executescript(sql)
        
        checksum = hashlib.sha256(sql.encode('utf-8')).hexdigest()
        from datetime import timezone
        cursor.execute(
            "INSERT INTO schema_migrations (migration_name, applied_at, checksum) VALUES (?, ?, ?)",
            (migration_name, datetime.now(timezone.utc).isoformat(), checksum)
        )
        conn.commit()
        print("Migration complete.")

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Apply shadow-review database migration.")
    parser.add_argument("--db", default=str(DATABASE_PATH), help="SQLite database path")
    args = parser.parse_args()
    apply_migration(Path(args.db))
