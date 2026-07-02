"""
config.py
=========
Single source of truth for all configurable thresholds, paths, and constants.

Do not scatter these values throughout the project.
Changing a threshold or path here affects the entire system.
"""
import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Root directory of the project (parent of this file's directory).
# All relative paths are anchored here so the project is portable.
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).parent.parent.resolve()

# ---------------------------------------------------------------------------
# Data paths (relative to project root)
# ---------------------------------------------------------------------------
DATA_DIR = PROJECT_ROOT / "data"
MODELS_DIR = PROJECT_ROOT / "models"
REPORTS_DIR = PROJECT_ROOT / "reports"
STORAGE_DIR = PROJECT_ROOT / "storage"

# ---------------------------------------------------------------------------
# Database and model file paths
# ---------------------------------------------------------------------------
DATABASE_PATH = Path(os.environ.get("TRADE_DB_PATH", STORAGE_DIR / "trade_holdings.db"))
MODEL_PATH = MODELS_DIR / "action_classifier.joblib"
MODEL_METADATA_PATH = MODELS_DIR / "model_metadata.json"

# ---------------------------------------------------------------------------
# Parser version (used in audit trail)
# ---------------------------------------------------------------------------
PARSER_VERSION = "trade-parser-v1"

# ---------------------------------------------------------------------------
# Portfolio defaults
# ---------------------------------------------------------------------------
DEFAULT_PORTFOLIO_ID = "default"
DEFAULT_ENTRY_ALLOCATION_PCT = "100.0"    # Decimal string – full model allocation
MAX_MODEL_ALLOCATION_PCT = "100.0"        # Decimal string – cap on any position

# Deprecated for v2 semantics: MAX_MODEL_ALLOCATION_PCT is no longer applied
# to entry/add exposure. Valid repeated entries may exceed 100 units.

# ---------------------------------------------------------------------------
# Reduction semantics
# ---------------------------------------------------------------------------
DEFAULT_PART_PROFIT_PCT = "25.0"          # "Part profit" → sell 25% of current holding
ZERO_POSITION_TOLERANCE = "0.000001"     # Allocation below this → treat as CLOSED

# ---------------------------------------------------------------------------
# Confidence thresholds
# Values below REVIEW_THRESHOLD go straight to rejection/manual review.
# Values in [REVIEW, AUTO_APPLY) go to manual review.
# Values >= AUTO_APPLY_THRESHOLD (and passing validation) are eligible for auto-apply.
# ---------------------------------------------------------------------------
AUTO_APPLY_THRESHOLD = 0.95
REVIEW_THRESHOLD = 0.75

# ---------------------------------------------------------------------------
# ML training settings
# ---------------------------------------------------------------------------
MIN_ML_CLASS_SAMPLES = 5   # Classes with fewer examples use rule fallback
RANDOM_STATE = 42

# ---------------------------------------------------------------------------
# Dataset file names (relative to DATA_DIR)
# ---------------------------------------------------------------------------
PRODUCTION_MESSAGES_FILE = "01_production_messages_final.csv"
TRAIN_READY_FILE = "03_model_train_ready_deduplicated.csv"
ANNOTATION_REVIEW_FILE = "04_annotation_review_queue.csv"
MANUAL_CONTEXT_FILE = "05_production_manual_or_context_queue.csv"
PILOT_TRAIN_FILE = "07_pilot_train_210.csv"
PILOT_VALIDATION_FILE = "08_pilot_validation_45.csv"
PILOT_TEST_FILE = "09_pilot_test_45.csv"
ALIASES_FILE = "10_instrument_aliases.csv"
RULES_FILE = "11_label_and_position_rules.csv"
SCHEMA_FILE = "12_dataset_schema.csv"

# ---------------------------------------------------------------------------
# Dataset column names
# ---------------------------------------------------------------------------
TEXT_COL = "normalized_text"
LABEL_COL = "action_label"
HASH_COL = "normalized_text_hash"

# ---------------------------------------------------------------------------
# Processing status codes used in processed_messages table
# ---------------------------------------------------------------------------
STATUS_APPLIED = "APPLIED"
STATUS_NON_TRADE = "NON_TRADE"
STATUS_STATUS_ONLY = "STATUS_ONLY"
STATUS_MANUAL_REVIEW = "MANUAL_REVIEW"
STATUS_REJECTED = "REJECTED"
STATUS_CONDITIONAL = "CONDITIONAL"
STATUS_CANCEL_PENDING = "CANCEL_PENDING"
STATUS_ALREADY_PROCESSED = "ALREADY_PROCESSED"
STATUS_DUPLICATE_SKIP = "DUPLICATE_SKIP"

# ---------------------------------------------------------------------------
# Path resolution helper
# ---------------------------------------------------------------------------
def resolve_input_path(value: str | Path) -> Path:
    supplied = Path(value)

    if supplied.is_absolute():
        candidate = supplied
    elif supplied.exists():
        candidate = supplied.resolve()
    else:
        parts = supplied.parts

        if parts and parts[0].lower() == "data":
            candidate = DATA_DIR.joinpath(*parts[1:])
        else:
            candidate = DATA_DIR / supplied

    candidate = candidate.resolve()

    if not candidate.exists():
        raise FileNotFoundError(
            f"Dataset not found: {candidate}"
        )

    return candidate
