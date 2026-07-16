"""Keep HTTP integration tests on the explicit test-only auth policy."""

from __future__ import annotations

import os

# Test modules intentionally exercise legacy route-local bypasses; pinning this
# prevents a developer's ignored .env from changing authorization semantics.
os.environ["APP_ENV"] = "test"
