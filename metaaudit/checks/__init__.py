"""Importing this package registers every check."""

from . import (  # noqa: F401
    creative,
    dropoff,
    efficiency,
    fatigue,
    fragmentation,
    learning,
    overlap,
    tracking,
    utm,
)
from .base import (  # noqa: F401
    CheckResult,
    Confidence,
    Finding,
    Severity,
    all_checks,
    run_all,
)
