"""Shared pytest configuration for the domain tests: a reproducible hypothesis profile.

hypothesis keeps small caches (constants, unicode data) in a storage directory even when the example
database is off. The directory is moved to a temporary location that is removed at exit, so a test run leaves
nothing in the working tree.
"""

from __future__ import annotations

import atexit
import tempfile

from hypothesis import settings
from hypothesis.configuration import set_hypothesis_home_dir

_storage = tempfile.TemporaryDirectory(prefix="studio-hypothesis-")
atexit.register(_storage.cleanup)
set_hypothesis_home_dir(_storage.name)

settings.register_profile("domain", database=None, derandomize=True, deadline=None)
settings.load_profile("domain")
