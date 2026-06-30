import pandas as pd
from pathlib import Path
from src.config import resolve_input_path, STORAGE_DIR, PRODUCTION_MESSAGES_FILE
from src.database import init_db
from src.replay import run_replay

def main():
    input_path = resolve_input_path(f"data/{PRODUCTION_MESSAGES_FILE}")
    db_path = STORAGE_DIR / "trade_holdings_audit.db"
    
    if db_path.exists(): db_path.unlink()
    init_db(db_path)
    
    s1, _, _, _ = run_replay(input_path, db_path, mode="safe-apply", verbose=False)
    print("Safe 1 applied:", s1.applied)
    
    s2, _, _, _ = run_replay(input_path, db_path, mode="safe-apply", verbose=False)
    print("Safe 2 applied:", s2.applied)
    
    s3, _, _, _ = run_replay(input_path, db_path, mode="dry-run", verbose=False)
    print("Dry applied:", s3.applied)
    
if __name__ == "__main__":
    main()
