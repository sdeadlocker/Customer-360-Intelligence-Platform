"""``python -m c360`` entry point.

Exists alongside the ``c360`` console script in ``pyproject.toml`` because the two become available
at
different times. The console script is written into the environment by an install, so it only
appears
after ``uv sync`` has been re-run; ``python -m c360`` works immediately from a source checkout. Both
dispatch into :func:`c360.cli.main`, so there is one argument parser and one exit-code convention.
"""

from __future__ import annotations

from c360.cli import main

raise SystemExit(main())
