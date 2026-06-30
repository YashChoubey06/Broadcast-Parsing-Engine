"""
scripts/init_database.py
========================
Standalone database initialisation script.

    python scripts/init_database.py
"""
import sys
from pathlib import Path

# Make src importable when run from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.database import init_db
from src.config import DATABASE_PATH

if __name__ == "__main__":
    init_db(DATABASE_PATH)
    print("Database ready.")
