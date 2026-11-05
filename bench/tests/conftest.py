"""Test set-up for the benchmark scripts: they are plain modules, so put their folder on the import path."""
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BENCH / "scripts"))
SYNTHETIC = BENCH / "data" / "synthetic"
