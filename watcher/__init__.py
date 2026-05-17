# watcher/__init__.py
from watcher.core import (
    watch,
    session,
    MemoryMode,
    StepResult,
    WatcherSession,
    DataFrameLike,
    BackendRegistry,
    BackendAdapter,
)
from watcher.exceptions import (
    WatcherError,
    WatcherWarning,
    ThresholdExceeded,
    BackendError,
    ConfigurationError,
)
from watcher.handlers import (
    HandlerBase,
    TerminalHandler,
    register_handler,
    deregister_handler,
)

__all__ = [
    "watch",
    "session",
    "MemoryMode",
    "StepResult",
    "WatcherSession",
    "DataFrameLike",
    "BackendRegistry",
    "BackendAdapter",
    "WatcherError",
    "WatcherWarning",
    "ThresholdExceeded",
    "BackendError",
    "ConfigurationError",
    "HandlerBase",
    "TerminalHandler",
    "register_handler",
    "deregister_handler",
]
