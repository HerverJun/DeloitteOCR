"""Load the stdlib storage contract from the same product source as the worker."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from packages.runtime import require_path

