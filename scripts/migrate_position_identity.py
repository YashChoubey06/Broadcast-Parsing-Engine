import hashlib
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from src.config import DATABASE_PATH
from src.database import get_connection
from src.position_identity import audit_duplicate_identity_keys, canonicalize_position
from src.schemas import PositionState


MIGRATION_NAME = "002_position_identity_indexes"


def _row_to_position(row: sqlite3.Row) -> PositionState:
    return PositionState(
        position_id=row["id"],
        portfolio_id=row["portfolio_id"],
        market_group=row["market_group"],
        symbol=row["symbol"],
        contract_month=row["contract_month"],
        option_type=row["option_type"],
        strike_price=row["strike_price"],
        direction=row["direction"],
        current_allocation_pct=row["current_allocation_pct"],
        average_entry_price=row["average_entry_price"],
        stop_loss=row["stop_loss"],
        status=row["status"],
        opened_at=row["opened_at"],
        updated_at=row["updated_at"],
        version=row["version"],
    )


def audit_position_identity_duplicates(db_path: Path = DATABASE_PATH) -> list[dict]:
    with closing(get_connection(db_path)) as conn:
        rows = conn.execute("SELECT * FROM positions ORDER BY id").fetchall()
        positions = [_row_to_position(row) for row in rows]
        return audit_duplicate_identity_keys(positions)


def apply_migration(db_path: Path | None = None) -> None:
    if db_path is None:
        db_path = DATABASE_PATH

    with closing(get_connection(db_path)) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                migration_id INTEGER PRIMARY KEY AUTOINCREMENT,
                migration_name TEXT NOT NULL UNIQUE,
                applied_at TEXT NOT NULL,
                checksum TEXT NOT NULL
            );
        """)

        cursor.execute(
            "SELECT 1 FROM schema_migrations WHERE migration_name = ?",
            (MIGRATION_NAME,),
        )
        if cursor.fetchone():
            print(f"Migration {MIGRATION_NAME} already applied.")
            return

        rows = cursor.execute("SELECT * FROM positions ORDER BY id").fetchall()
        normalized_positions = []
        for row in rows:
            pos = canonicalize_position(_row_to_position(row))
            normalized_positions.append(pos)

        duplicates = audit_duplicate_identity_keys(normalized_positions)
        if duplicates:
            details = "; ".join(
                f"key={item['identity_key']} ids={item['position_ids']}"
                for item in duplicates
            )
            raise RuntimeError(
                "Duplicate normalized position identities found; refusing to "
                f"create unique index. {details}"
            )

        sql = """
            UPDATE positions
            SET market_group = ?, symbol = ?, contract_month = ?,
                option_type = ?, strike_price = ?, direction = ?
            WHERE id = ?;

            CREATE INDEX IF NOT EXISTS idx_positions_phase2_full_identity
            ON positions (
                portfolio_id, market_group, symbol, contract_month,
                option_type, strike_price, direction
            );

            CREATE UNIQUE INDEX IF NOT EXISTS uq_positions_phase2_full_identity
            ON positions (
                portfolio_id, market_group, symbol, contract_month,
                option_type, strike_price, direction
            );
        """

        try:
            cursor.execute("BEGIN IMMEDIATE")
            for pos in normalized_positions:
                cursor.execute(
                    """
                    UPDATE positions
                    SET market_group = ?, symbol = ?, contract_month = ?,
                        option_type = ?, strike_price = ?, direction = ?
                    WHERE id = ?
                    """,
                    (
                        pos.market_group,
                        pos.symbol,
                        pos.contract_month,
                        pos.option_type,
                        str(pos.strike_price) if pos.strike_price else "",
                        pos.direction,
                        pos.position_id,
                    ),
                )
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_positions_phase2_full_identity
                ON positions (
                    portfolio_id, market_group, symbol, contract_month,
                    option_type, strike_price, direction
                )
            """)
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_positions_phase2_full_identity
                ON positions (
                    portfolio_id, market_group, symbol, contract_month,
                    option_type, strike_price, direction
                )
            """)
            checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            cursor.execute(
                """
                INSERT INTO schema_migrations (migration_name, applied_at, checksum)
                VALUES (?, ?, ?)
                """,
                (MIGRATION_NAME, datetime.now(timezone.utc).isoformat(), checksum),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        print(f"Migration {MIGRATION_NAME} complete.")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Apply Phase 2 position identity migration.")
    parser.add_argument("--db", default=str(DATABASE_PATH), help="SQLite database path")
    args = parser.parse_args()
    apply_migration(Path(args.db))
