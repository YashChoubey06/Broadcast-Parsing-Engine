import pytest
from pathlib import Path
from src.config import resolve_input_path, DATA_DIR

def test_resolve_input_path(monkeypatch):
    # Setup test file
    test_file = DATA_DIR / "07_pilot_train_210.csv"
    if not test_file.exists():
        test_file.touch()

    # 1. Filename relative to DATA_DIR
    p1 = resolve_input_path("07_pilot_train_210.csv")
    assert p1 == test_file.resolve()

    # 2. Paths beginning with data/
    p2 = resolve_input_path("data/07_pilot_train_210.csv")
    assert p2 == test_file.resolve()

    p3 = resolve_input_path(r"data\07_pilot_train_210.csv")
    assert p3 == test_file.resolve()

    # 3. Absolute path
    p4 = resolve_input_path(str(test_file.resolve()))
    assert p4 == test_file.resolve()

    # 4. Path relative to current project directory
    # Assuming tests run from project root
    p5 = resolve_input_path(r".\data\07_pilot_train_210.csv")
    assert p5 == test_file.resolve()

    # Check non-existent file
    with pytest.raises(FileNotFoundError):
        resolve_input_path("nonexistent_file_xyz.csv")
