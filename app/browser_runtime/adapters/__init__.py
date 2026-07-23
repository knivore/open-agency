"""Private engine adapters used by the durable browser runtime."""

from .base import BrowserEngineAdapter, EngineNavigationError, EngineUnavailableError
from .patchright import PatchrightAdapter
from .scrapling import ScraplingAdapter

__all__ = [
    "BrowserEngineAdapter",
    "EngineNavigationError",
    "EngineUnavailableError",
    "PatchrightAdapter",
    "ScraplingAdapter",
]

