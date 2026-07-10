"""Main-agent setup package.

The package root is the stable import surface for scripts and startup code;
the concrete implementation lives in `service.py` so setup internals can stay
grouped without leaking the file layout to callers.
"""

from .service import (
    MainAgentModelProfileRequiredError,
    MainAgentSetupConfig,
    MainAgentSetupError,
    MainAgentSetupInvalidError,
    MainAgentSetupRequiredError,
    MainAgentSetupService,
)

__all__ = [
    "MainAgentModelProfileRequiredError",
    "MainAgentSetupConfig",
    "MainAgentSetupError",
    "MainAgentSetupInvalidError",
    "MainAgentSetupRequiredError",
    "MainAgentSetupService",
]
