"""Convenience entry point: `pipenv run python main.py` runs the weekly
pipeline. Equivalent to `PYTHONPATH=src pipenv run python -m skip_tracer.cli`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from skip_tracer.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
