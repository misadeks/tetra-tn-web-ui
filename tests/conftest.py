import sys
from pathlib import Path

# Ensure the repo root is importable (app package + fake_stack module).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
