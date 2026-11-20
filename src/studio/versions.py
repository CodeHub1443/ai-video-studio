"""Single source of truth for on-disk schema versions (registry YAML and hardware profiles).

Bump ``SCHEMA_VERSION`` and the ``SchemaVersion`` literal together; every spec model refers to both, so a
file written for another version is rejected instead of being half-understood.
"""

from __future__ import annotations

from typing import Final, Literal

SCHEMA_VERSION: Final = 1
SchemaVersion = Literal[1]
