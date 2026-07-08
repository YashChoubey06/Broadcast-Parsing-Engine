import os
from datetime import datetime, timezone
from pathlib import Path

from streamlit.testing.v1 import AppTest

from scripts.migrate_shadow_tables import apply_migration
from src.database import get_connection, init_db
from src.schemas import ParsedTradeEvent


def test_shadow_review_app_renders_unknown_prediction(tmp_path, monkeypatch):
    db_path = tmp_path / "unknown_prediction.db"
    init_db(db_path)
    apply_migration(db_path)

    now = datetime.now(timezone.utc).isoformat()
    event = ParsedTradeEvent(
        source_message_id="unknown-1",
        raw_text="legacy unknown prediction",
        final_action="UNKNOWN",
        record_type="UNKNOWN",
    )
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO incoming_messages (
                source_message_id, raw_text, normalized_text, segment_name,
                created_at, received_at, processing_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "unknown-1",
                "legacy unknown prediction",
                "legacy unknown prediction",
                "Unknown",
                now,
                now,
                "PENDING_REVIEW",
            ),
        )
        conn.execute(
            """
            INSERT INTO parser_predictions (
                source_message_id, parser_version, model_version, prediction_json,
                final_action, validation_status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("unknown-1", "v1", "v1", event.to_json(), "UNKNOWN", "INVALID", now),
        )
        conn.commit()

    monkeypatch.setenv("TRADE_DB_PATH", str(db_path))
    import src.config as config

    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    app = AppTest.from_file(str(Path("app") / "shadow_review_app.py"))
    app.session_state["selected_msg"] = "unknown-1"
    app.run(timeout=10)

    assert not app.exception
    assert any("legacy unknown prediction" in code.value for code in app.code)
    assert any("UNKNOWN" in option for selectbox in app.selectbox for option in selectbox.options)
